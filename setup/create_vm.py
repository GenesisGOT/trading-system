"""
Oracle Cloud Always Free VM setup script.
Creates an Ampere A1 VM (4 OCPU / 24 GB) with Docker pre-installed,
opens port 8000, and deploys the trading system automatically.

Usage:
    python setup/create_vm.py

Requires OCI CLI to be configured first (run: oci session authenticate)
"""
import oci
import time
import sys
import subprocess
import os

REGION = "us-ashburn-1"
SHAPE = "VM.Standard.A1.Flex"
OCPUS = 4
MEMORY_GB = 24
DISPLAY_NAME = "trading-system"
SSH_KEY_PATH = os.path.expanduser("~/.ssh/trading_system_rsa")

CLOUD_INIT = """#!/bin/bash
set -e
apt-get update -qq
curl -fsSL https://get.docker.com | sh
usermod -aG docker ubuntu
apt-get install -y docker-compose-plugin git

# Open port 8000 in OS firewall
iptables -I INPUT -p tcp --dport 8000 -j ACCEPT
iptables -I INPUT -p tcp --dport 22 -j ACCEPT

# Deploy trading system
sudo -u ubuntu bash -c '
  cd /home/ubuntu
  git clone https://github.com/GenesisGOT/trading-system.git
  cd trading-system
  cp .env.example .env
  # Set your GROQ_API_KEY in .env manually
  docker compose up -d --build
'

echo "Trading system deployed at port 8000"
"""


def generate_ssh_key():
    if not os.path.exists(SSH_KEY_PATH):
        print("Generating SSH key pair...")
        subprocess.run(
            ["ssh-keygen", "-t", "rsa", "-b", "4096", "-f", SSH_KEY_PATH, "-N", ""],
            check=True, capture_output=True
        )
        print(f"SSH key saved to {SSH_KEY_PATH}")
    with open(f"{SSH_KEY_PATH}.pub") as f:
        return f.read().strip()


def get_availability_domain(identity_client, tenancy_id):
    ads = identity_client.list_availability_domains(tenancy_id).data
    # Prefer AD-1 for free tier
    return ads[0].name


def get_or_create_vcn(network_client, compartment_id):
    vcns = network_client.list_vcns(compartment_id, display_name=DISPLAY_NAME).data
    if vcns:
        print(f"Using existing VCN: {vcns[0].id}")
        return vcns[0]

    print("Creating VCN...")
    vcn = network_client.create_vcn(oci.core.models.CreateVcnDetails(
        cidr_block="10.0.0.0/16",
        compartment_id=compartment_id,
        display_name=DISPLAY_NAME,
    )).data
    time.sleep(3)
    return vcn


def get_or_create_subnet(network_client, compartment_id, vcn_id, ad_name):
    subnets = network_client.list_subnets(compartment_id, vcn_id=vcn_id).data
    if subnets:
        return subnets[0]

    # Internet Gateway
    ig = network_client.create_internet_gateway(oci.core.models.CreateInternetGatewayDetails(
        compartment_id=compartment_id,
        vcn_id=vcn_id,
        is_enabled=True,
        display_name=DISPLAY_NAME,
    )).data
    time.sleep(2)

    # Route table
    rt = network_client.create_route_table(oci.core.models.CreateRouteTableDetails(
        compartment_id=compartment_id,
        vcn_id=vcn_id,
        display_name=DISPLAY_NAME,
        route_rules=[oci.core.models.RouteRule(
            network_entity_id=ig.id,
            destination="0.0.0.0/0",
        )],
    )).data
    time.sleep(2)

    # Security list — allow SSH + port 8000 inbound, all outbound
    sl = network_client.create_security_list(oci.core.models.CreateSecurityListDetails(
        compartment_id=compartment_id,
        vcn_id=vcn_id,
        display_name=DISPLAY_NAME,
        ingress_security_rules=[
            oci.core.models.IngressSecurityRule(
                protocol="6",  # TCP
                source="0.0.0.0/0",
                tcp_options=oci.core.models.TcpOptions(
                    destination_port_range=oci.core.models.PortRange(min=22, max=22)
                ),
            ),
            oci.core.models.IngressSecurityRule(
                protocol="6",
                source="0.0.0.0/0",
                tcp_options=oci.core.models.TcpOptions(
                    destination_port_range=oci.core.models.PortRange(min=8000, max=8000)
                ),
            ),
        ],
        egress_security_rules=[
            oci.core.models.EgressSecurityRule(
                protocol="all",
                destination="0.0.0.0/0",
            )
        ],
    )).data
    time.sleep(2)

    subnet = network_client.create_subnet(oci.core.models.CreateSubnetDetails(
        availability_domain=ad_name,
        cidr_block="10.0.0.0/24",
        compartment_id=compartment_id,
        vcn_id=vcn_id,
        display_name=DISPLAY_NAME,
        route_table_id=rt.id,
        security_list_ids=[sl.id],
    )).data
    time.sleep(3)
    return subnet


def get_ubuntu_image(compute_client, compartment_id):
    images = compute_client.list_images(
        compartment_id,
        operating_system="Canonical Ubuntu",
        operating_system_version="22.04",
        shape=SHAPE,
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data
    if not images:
        raise RuntimeError("No Ubuntu 22.04 ARM image found for this region")
    return images[0].id


def create_instance(compute_client, compartment_id, ad_name, subnet_id, image_id, ssh_pub_key):
    # Try largest free config first, fall back to smaller if capacity unavailable
    # Oracle free tier capacity comes and goes — retry patiently every 5 minutes.
    # Try largest config first (4/24), fall back after 3 failures each.
    configs = [(4, 24), (2, 12), (1, 6)]
    attempt = 0

    while True:
        ocpus, mem = configs[min(attempt // 3, len(configs) - 1)]
        attempt += 1
        wait = 300 + (attempt * 10)  # 5 min + small backoff per attempt
        print(f"[Attempt {attempt}] Requesting {ocpus} OCPU / {mem}GB in {ad_name}...")
        try:
            instance = compute_client.launch_instance(oci.core.models.LaunchInstanceDetails(
                availability_domain=ad_name,
                compartment_id=compartment_id,
                display_name=DISPLAY_NAME,
                shape=SHAPE,
                shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                    ocpus=ocpus,
                    memory_in_gbs=mem,
                ),
                source_details=oci.core.models.InstanceSourceViaImageDetails(
                    image_id=image_id,
                    source_type="image",
                ),
                create_vnic_details=oci.core.models.CreateVnicDetails(
                    subnet_id=subnet_id,
                    assign_public_ip=True,
                ),
                metadata={
                    "ssh_authorized_keys": ssh_pub_key,
                    "user_data": __import__("base64").b64encode(CLOUD_INIT.encode()).decode(),
                },
            )).data
            print(f"Instance launched: {ocpus} OCPU / {mem}GB")
            return instance
        except oci.exceptions.ServiceError as e:
            if e.status in (429, 500) or "Out of host capacity" in str(e):
                print(f"  No capacity / rate limited — waiting {wait}s then retrying (Ctrl+C to stop)")
                time.sleep(wait)
            else:
                raise


def wait_for_instance(compute_client, instance_id):
    print("Waiting for VM to start", end="", flush=True)
    while True:
        inst = compute_client.get_instance(instance_id).data
        if inst.lifecycle_state == "RUNNING":
            print(" RUNNING")
            return inst
        elif inst.lifecycle_state in ("TERMINATED", "TERMINATING", "FAILED"):
            raise RuntimeError(f"Instance failed: {inst.lifecycle_state}")
        print(".", end="", flush=True)
        time.sleep(10)


def get_public_ip(compute_client, network_client, instance_id, compartment_id):
    vnics = compute_client.list_vnic_attachments(compartment_id, instance_id=instance_id).data
    for v in vnics:
        vnic = network_client.get_vnic(v.vnic_id).data
        if vnic.public_ip:
            return vnic.public_ip
    return None


KEY_FILE = r"C:\Users\lilgu\.oci\oci_api_key_rsa.pem"
CONFIG = {
    "user":        "ocid1.user.oc1..aaaaaaaayljzveaatubn6fgha36ffljsv3ux4mkyphvq7tzo2zlydtemxvfa",
    "fingerprint": "fd:05:03:34:db:1b:b1:2e:95:44:b4:03:94:4a:1f:ea",  # matches Oracle-uploaded key
    "tenancy":     "ocid1.tenancy.oc1..aaaaaaaabtpyggvf7runysrjz7nufnxsnakdi7htg4exanpxzv2swyd32sjq",
    "region":      "us-sanjose-1",
    "key_file":    KEY_FILE,
}


def main():
    print("=== Oracle Always Free VM Setup ===\n")

    config = CONFIG
    oci.config.validate_config(config)
    compartment_id = config["tenancy"]
    region = config["region"]
    print(f"Tenancy: {compartment_id[:30]}... Region: {region}")

    # Generate SSH key
    ssh_pub_key = generate_ssh_key()

    # Explicit signer with hardcoded key path — avoids ~ expansion issues on Windows
    with open(KEY_FILE) as f:
        private_key_content = f.read()
    signer = oci.signer.Signer(
        tenancy=config["tenancy"],
        user=config["user"],
        fingerprint=config["fingerprint"],
        private_key_file_location=KEY_FILE,
    )
    identity = oci.identity.IdentityClient(config, signer=signer)
    compute = oci.core.ComputeClient(config, signer=signer)
    network = oci.core.VirtualNetworkClient(config, signer=signer)

    ad_name = get_availability_domain(identity, compartment_id)
    print(f"Availability Domain: {ad_name}")

    vcn = get_or_create_vcn(network, compartment_id)
    subnet = get_or_create_subnet(network, compartment_id, vcn.id, ad_name)
    image_id = get_ubuntu_image(compute, compartment_id)
    print(f"Ubuntu 22.04 ARM image: {image_id[:30]}...")

    instance = create_instance(compute, compartment_id, ad_name, subnet.id, image_id, ssh_pub_key)
    print(f"Instance ID: {instance.id}")

    instance = wait_for_instance(compute, instance.id)
    public_ip = get_public_ip(compute, network, instance.id, compartment_id)

    print(f"\n{'='*50}")
    print(f"VM is UP at: {public_ip}")
    print(f"SSH:  ssh -i {SSH_KEY_PATH} ubuntu@{public_ip}")
    print(f"API:  http://{public_ip}:8000/health  (ready in ~5 min)")
    print(f"{'='*50}\n")
    print("The trading system is deploying via cloud-init.")
    print("Check progress: ssh -i ~/.ssh/trading_system_rsa ubuntu@" + public_ip + " 'tail -f /var/log/cloud-init-output.log'")

    # Save IP to .env note
    with open("C:/Users/lilgu/trading-system/.vm_ip", "w") as f:
        f.write(public_ip)


if __name__ == "__main__":
    main()
