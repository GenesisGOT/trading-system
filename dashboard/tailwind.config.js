/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        terminal: {
          bg:      '#080b0f',
          surface: '#0d1117',
          card:    '#111820',
          border:  '#1e2d3d',
          green:   '#00ff88',
          cyan:    '#00d4ff',
          yellow:  '#ffd700',
          red:     '#ff4757',
          muted:   '#4a6075',
          text:    '#c9d1d9',
        },
      },
      fontFamily: {
        mono: ['"JetBrains Mono"', '"Fira Code"', 'Consolas', 'monospace'],
      },
      animation: {
        'pulse-slow': 'pulse 3s cubic-bezier(0.4,0,0.6,1) infinite',
        'blink': 'blink 1s step-end infinite',
      },
      keyframes: {
        blink: { '0%,100%': { opacity: '1' }, '50%': { opacity: '0' } },
      },
    },
  },
  plugins: [],
}
