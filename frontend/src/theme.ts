export type Theme = 'light' | 'dark';

const KEY = 'v2-board-theme';
const CHROME: Record<Theme, string> = { light: '#faf9f5', dark: '#181818' };

export function storedTheme(): Theme {
  try {
    return localStorage.getItem(KEY) === 'dark' ? 'dark' : 'light';
  } catch {
    return 'light';
  }
}

export function applyTheme(theme: Theme, remember = false) {
  document.documentElement.dataset.theme = theme;
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', CHROME[theme]);
  if (!remember) return;
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    /* 存不了就只在本次打开期间生效 */
  }
}
