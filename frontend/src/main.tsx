import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import Gate from './Login';
import { applyTheme, storedTheme } from './theme';
import './theme.css';
import './styles.css';

applyTheme(storedTheme());

createRoot(document.getElementById('root')!).render(<StrictMode><Gate /></StrictMode>);
