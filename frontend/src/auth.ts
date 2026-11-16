type Mode = 'login' | 'register';

interface Session {
  token: string;
  username: string;
}

const storageKey = 'flash.session';
const nativeFetch = window.fetch.bind(window);

let session: Session | null = readSession();
let bar: HTMLElement | null = null;
let overlay: HTMLElement | null = null;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function readSession(): Session | null {
  try {
    const raw = window.sessionStorage.getItem(storageKey);
    if (!raw) {
      return null;
    }
    const value: unknown = JSON.parse(raw);
    if (isRecord(value) && typeof value.token === 'string' && typeof value.username === 'string') {
      return { token: value.token, username: value.username };
    }
  } catch {
    return null;
  }
  return null;
}

function writeSession(next: Session | null): void {
  session = next;
  try {
    if (next) {
      window.sessionStorage.setItem(storageKey, JSON.stringify(next));
    } else {
      window.sessionStorage.removeItem(storageKey);
    }
  } catch {
    // El almacenamiento puede no estar disponible; la sesion vive en memoria.
  }
  renderBar();
}

function node<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className?: string,
  text?: string
): HTMLElementTagNameMap[K] {
  const element = document.createElement(tag);
  if (className) {
    element.className = className;
  }
  if (text !== undefined) {
    element.textContent = text;
  }
  return element;
}

function describeRequest(
  input: RequestInfo | URL,
  init?: RequestInit
): { path: string; method: string; sameOrigin: boolean } {
  const raw = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
  const url = new URL(raw, window.location.href);
  const method = (init?.method ?? (input instanceof Request ? input.method : 'GET')).toUpperCase();
  return { path: url.pathname, method, sameOrigin: url.origin === window.location.origin };
}

window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const { path, method, sameOrigin } = describeRequest(input, init);
  if (!sameOrigin || !path.startsWith('/api/v1/')) {
    return nativeFetch(input, init);
  }
  if (method === 'POST' && path === '/api/v1/orders' && session === null) {
    openModal('login', 'Inicia sesi\u00f3n para hacer tu pedido.');
    const body = { error: { code: 'UNAUTHORIZED', message: 'Inicia sesi\u00f3n para hacer tu pedido.' } };
    return Promise.resolve(
      new Response(JSON.stringify(body), {
        status: 401,
        headers: { 'Content-Type': 'application/json' }
      })
    );
  }
  if (session === null) {
    return nativeFetch(input, init);
  }
  const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined));
  headers.set('Authorization', `Bearer ${session.token}`);
  return nativeFetch(input, { ...init, headers }).then((response) => {
    if (response.status === 401 && session !== null) {
      writeSession(null);
      openModal('login', 'Tu sesi\u00f3n venci\u00f3. Inicia sesi\u00f3n de nuevo.');
    }
    return response;
  });
};

function friendlyAuthError(status: number, code: string): string {
  if (code === 'INVALID_CREDENTIALS') {
    return 'Usuario o contrase\u00f1a incorrectos.';
  }
  if (code === 'USERNAME_TAKEN') {
    return 'Ese nombre de usuario ya existe.';
  }
  if (status === 429) {
    return 'Demasiados intentos. Espera unos minutos.';
  }
  if (status === 422) {
    return 'Usuario: 3 a 32 letras, n\u00fameros o _. Contrase\u00f1a: m\u00ednimo 8 caracteres.';
  }
  return 'No se pudo completar la solicitud. Intenta de nuevo.';
}

async function submitCredentials(
  mode: Mode,
  username: string,
  password: string
): Promise<{ ok: true; session: Session } | { ok: false; message: string }> {
  try {
    const response = await nativeFetch(`/api/v1/auth/${mode}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ username, password })
    });
    const data: unknown = await response.json().catch(() => null);
    if (response.ok && isRecord(data) && typeof data.token === 'string' && isRecord(data.user)) {
      const name = typeof data.user.username === 'string' ? data.user.username : username;
      return { ok: true, session: { token: data.token, username: name } };
    }
    const error = isRecord(data) && isRecord(data.error) ? data.error : {};
    const code = typeof error.code === 'string' ? error.code : '';
    return { ok: false, message: friendlyAuthError(response.status, code) };
  } catch {
    return { ok: false, message: 'No se pudo conectar con el servidor.' };
  }
}

function closeModal(): void {
  if (overlay) {
    overlay.remove();
    overlay = null;
    document.removeEventListener('keydown', onEscape);
  }
}

function onEscape(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    closeModal();
  }
}

function openModal(mode: Mode, message = ''): void {
  closeModal();
  const isLogin = mode === 'login';

  const dialog = node('div', 'auth-dialog');
  dialog.setAttribute('role', 'dialog');
  dialog.setAttribute('aria-modal', 'true');
  dialog.setAttribute('aria-labelledby', 'auth-title');

  const title = node('h2', 'auth-title', isLogin ? 'Iniciar sesi\u00f3n' : 'Crear cuenta');
  title.id = 'auth-title';
  const intro = node('p', 'auth-intro', message || (isLogin ? 'Entra para hacer pedidos.' : 'Crea tu cuenta de demostraci\u00f3n.'));

  const form = node('form', 'auth-form');
  const userLabel = node('label', 'auth-label', 'Usuario');
  const userInput = node('input', 'auth-input');
  userInput.type = 'text';
  userInput.name = 'username';
  userInput.autocomplete = 'username';
  userInput.required = true;
  userInput.minLength = 3;
  userInput.maxLength = 32;
  userLabel.append(userInput);

  const passLabel = node('label', 'auth-label', 'Contrase\u00f1a');
  const passInput = node('input', 'auth-input');
  passInput.type = 'password';
  passInput.name = 'password';
  passInput.autocomplete = isLogin ? 'current-password' : 'new-password';
  passInput.required = true;
  passInput.minLength = 8;
  passInput.maxLength = 128;
  passLabel.append(passInput);

  const error = node('p', 'auth-error');
  error.setAttribute('role', 'alert');
  error.hidden = true;

  const submit = node('button', 'button button-primary', isLogin ? 'Entrar' : 'Crear cuenta');
  submit.type = 'submit';

  const switchButton = node(
    'button',
    'auth-link',
    isLogin ? '\u00bfNo tienes cuenta? Reg\u00edstrate' : '\u00bfYa tienes cuenta? Inicia sesi\u00f3n'
  );
  switchButton.type = 'button';
  switchButton.addEventListener('click', () => {
    openModal(isLogin ? 'register' : 'login', message);
  });

  const cancel = node('button', 'auth-link', 'Cancelar');
  cancel.type = 'button';
  cancel.addEventListener('click', closeModal);

  const footer = node('div', 'auth-footer');
  footer.append(switchButton, cancel);

  form.append(userLabel, passLabel, error, submit, footer);
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    submit.disabled = true;
    error.hidden = true;
    void submitCredentials(mode, userInput.value.trim(), passInput.value).then((result) => {
      submit.disabled = false;
      if (result.ok) {
        writeSession(result.session);
        closeModal();
        return;
      }
      error.textContent = result.message;
      error.hidden = false;
      passInput.value = '';
      passInput.focus();
    });
  });

  dialog.append(title, intro, form);
  overlay = node('div', 'auth-overlay');
  overlay.addEventListener('click', (event) => {
    if (event.target === overlay) {
      closeModal();
    }
  });
  overlay.append(dialog);
  document.body.append(overlay);
  document.addEventListener('keydown', onEscape);
  userInput.focus();
}

function logout(): void {
  const token = session?.token;
  writeSession(null);
  if (token) {
    void nativeFetch('/api/v1/auth/logout', {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` }
    }).catch(() => undefined);
  }
}

function renderBar(): void {
  if (!bar) {
    return;
  }
  if (session) {
    const label = node('span', 'auth-user', `Hola, ${session.username}`);
    const button = node('button', 'auth-bar-button', 'Salir');
    button.type = 'button';
    button.addEventListener('click', logout);
    bar.replaceChildren(label, button);
  } else {
    const button = node('button', 'auth-bar-button', 'Iniciar sesi\u00f3n');
    button.type = 'button';
    button.addEventListener('click', () => {
      openModal('login');
    });
    bar.replaceChildren(button);
  }
}

function mountBar(): void {
  if (bar) {
    return;
  }
  bar = node('div', 'auth-bar');
  bar.setAttribute('role', 'region');
  bar.setAttribute('aria-label', 'Sesi\u00f3n de usuario');
  document.body.append(bar);
  renderBar();
  if (session) {
    void nativeFetch('/api/v1/auth/me', {
      headers: { Authorization: `Bearer ${session.token}`, Accept: 'application/json' }
    })
      .then((response) => {
        if (response.status === 401) {
          writeSession(null);
        }
      })
      .catch(() => undefined);
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', mountBar, { once: true });
} else {
  mountBar();
}

export {};
