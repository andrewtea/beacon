// The app's icons: one stroke weight, drawn on a 24-unit grid.

const PATHS = {
  mic: 'M12 3.5a3 3 0 0 0-3 3v5a3 3 0 0 0 6 0v-5a3 3 0 0 0-3-3ZM5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v2.5',
  camera:
    'M4.5 8.5h3l1.5-2.5h6l1.5 2.5h3v10h-15ZM12 16.5a3.25 3.25 0 1 0 0-6.5 3.25 3.25 0 0 0 0 6.5Z',
  back: 'M14.5 5.5 8 12l6.5 6.5',
  forward: 'M9.5 5.5 16 12l-6.5 6.5',
  external: 'M13.5 5h5.5v5.5M19 5l-8 8M10 6.5H5.5v12h12V14',
  close: 'M6 6l12 12M18 6 6 18',
  pin: 'M12 21s6.5-6 6.5-11a6.5 6.5 0 0 0-13 0c0 5 6.5 11 6.5 11ZM12 12.25a2.25 2.25 0 1 0 0-4.5 2.25 2.25 0 0 0 0 4.5Z',
  radio:
    'M12 13.5V21M8.2 9.7a5.4 5.4 0 0 0 0 7.6M15.8 9.7a5.4 5.4 0 0 1 0 7.6M5.3 6.8a9.5 9.5 0 0 0 0 13.4M18.7 6.8a9.5 9.5 0 0 1 0 13.4',
  document: 'M7 3.5h7l4 4v13H7ZM14 3.5v4h4M9.5 12h6M9.5 15.5h6',
  alert: 'M12 4 3.5 19.5h17ZM12 10v4.5M12 17v.5',
  road: 'M8.5 3.5 5 20.5M15.5 3.5 19 20.5M12 5v2.5M12 10.5v3M12 16.5v3',
  aircraft:
    'M3.5 8.5h17M12 8.5v3M7 11.5h8.5a3 3 0 0 1 0 6H9.5L7 15ZM11 17.5v2.5M8 20h7',
  tap: 'M12 12.75a.75.75 0 1 0 0-1.5.75.75 0 0 0 0 1.5ZM8.1 8.1a5.5 5.5 0 0 0 0 7.8M15.9 8.1a5.5 5.5 0 0 1 0 7.8',
  trace:
    'M6.5 6.5a1.75 1.75 0 1 0 0-3.5 1.75 1.75 0 0 0 0 3.5ZM6.5 21a1.75 1.75 0 1 0 0-3.5 1.75 1.75 0 0 0 0 3.5ZM17.5 13.75a1.75 1.75 0 1 0 0-3.5 1.75 1.75 0 0 0 0 3.5ZM6.5 6.5v11M6.5 12h9.25',
  clock: 'M12 20.5a8.5 8.5 0 1 0 0-17 8.5 8.5 0 0 0 0 17ZM12 7.5V12l3 2',
} as const

export type IconName = keyof typeof PATHS

export function Icon({ name, size = 20 }: { name: IconName; size?: number }) {
  return (
    <svg
      className="icon"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d={PATHS[name]} />
    </svg>
  )
}
