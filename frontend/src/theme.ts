export type Theme = 'light' | 'dark';

const KEY = 'v2-board-theme';
const CHROME: Record<Theme, string> = { light: '#faf9f5', dark: '#181818' };
const LOGO: Record<Theme, string> = { light: '%23D97757', dark: '%23339CFF' };
// 与 index.html 里的默认图标是同一张图，只是底色随主题变
const favicon = (fill: string) => `data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Crect width='24' height='24' rx='6' fill='${fill}'/%3E%3Cpath d='M12 6 6 9l6 3 6-3-6-3Zm-6 6 6 3 6-3M6 15l6 3 6-3' fill='none' stroke='white' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E`;

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
  const icon = document.querySelector<HTMLLinkElement>('link[rel="icon"]');
  if (icon) icon.href = favicon(LOGO[theme]);
  if (!remember) return;
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    /* 存不了就只在本次打开期间生效 */
  }
}
