type BillingState = 'loading' | 'issued' | 'failed' | 'stalled' | 'missing';

interface BillingItem {
  name: string;
  quantity: number;
  unit_price_cents: number;
}

interface Billing {
  order_id: string;
  customer_name: string | null;
  status: 'pending' | 'issued' | 'failed';
  invoice_number: string | null;
  items: BillingItem[];
  total_cents: number;
  issued_at: string | null;
}

type FetchResult = { kind: 'ok'; billing: Billing } | { kind: 'missing' } | { kind: 'error' };

interface Entry {
  state: BillingState;
  billing: Billing | null;
  attempts: number;
  timer: number | null;
  controller: AbortController | null;
}

const entries = new Map<string, Entry>();
const pollIntervalMs = 2000;
const maxAttempts = 30;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function parseBilling(value: unknown): Billing | null {
  if (!isRecord(value)) {
    return null;
  }
  const status = value.status;
  if (status !== 'pending' && status !== 'issued' && status !== 'failed') {
    return null;
  }
  if (!Array.isArray(value.items) || typeof value.total_cents !== 'number') {
    return null;
  }
  const items: BillingItem[] = [];
  for (const raw of value.items) {
    if (
      !isRecord(raw) ||
      typeof raw.name !== 'string' ||
      typeof raw.quantity !== 'number' ||
      typeof raw.unit_price_cents !== 'number'
    ) {
      return null;
    }
    items.push({ name: raw.name, quantity: raw.quantity, unit_price_cents: raw.unit_price_cents });
  }
  return {
    order_id: typeof value.order_id === 'string' ? value.order_id : '',
    customer_name: typeof value.customer_name === 'string' ? value.customer_name : null,
    status,
    invoice_number: typeof value.invoice_number === 'string' ? value.invoice_number : null,
    items,
    total_cents: value.total_cents,
    issued_at: typeof value.issued_at === 'string' ? value.issued_at : null
  };
}

async function fetchBilling(orderId: string, signal: AbortSignal): Promise<FetchResult> {
  try {
    const response = await fetch(`/api/v1/orders/${encodeURIComponent(orderId)}/billing`, {
      headers: { Accept: 'application/json' },
      cache: 'no-store',
      signal
    });
    if (response.status === 404) {
      return { kind: 'missing' };
    }
    if (!response.ok) {
      return { kind: 'error' };
    }
    const billing = parseBilling(await response.json());
    return billing ? { kind: 'ok', billing } : { kind: 'error' };
  } catch {
    return { kind: 'error' };
  }
}

const moneyFormat = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2
});

function money(cents: number): string {
  return `S/ ${moneyFormat.format(cents / 100)}`;
}

function dateTime(iso: string | null): string {
  if (!iso) {
    return '\u2014';
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return '\u2014';
  }
  return new Intl.DateTimeFormat('es-PE', {
    day: 'numeric',
    month: 'long',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit'
  }).format(date);
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

function loadingBox(): HTMLElement {
  const box = node('div', 'invoice-notice invoice-notice-pending');
  box.setAttribute('role', 'status');
  const spinner = node('span', 'spinner');
  spinner.setAttribute('aria-hidden', 'true');
  const title = node('p', 'invoice-notice-title');
  title.append(spinner, 'Generando comprobante\u2026');
  box.append(
    title,
    node(
      'p',
      'invoice-notice-text',
      'Tu pedido ya est\u00e1 confirmado. El comprobante aparecer\u00e1 aqu\u00ed en unos segundos.'
    )
  );
  return box;
}

function warningBox(orderId: string, title: string, text: string): HTMLElement {
  const box = node('div', 'invoice-notice invoice-notice-warn');
  box.setAttribute('role', 'status');
  box.append(node('p', 'invoice-notice-title', title), node('p', 'invoice-notice-text', text));
  const button = node('button', 'button button-secondary', 'Consultar de nuevo');
  button.type = 'button';
  button.addEventListener('click', () => {
    retry(orderId);
  });
  box.append(button);
  return box;
}

function invoiceCard(billing: Billing): HTMLElement {
  const card = node('article', 'invoice');
  card.setAttribute('aria-label', 'Comprobante de venta');

  const titleBlock = node('div');
  titleBlock.append(
    node('h4', 'invoice-title', 'Comprobante de venta'),
    node('p', 'invoice-number', billing.invoice_number ?? 'Sin n\u00famero')
  );
  const head = node('div', 'invoice-head');
  head.append(titleBlock, node('span', 'invoice-badge', 'Emitido'));

  const meta = node('dl', 'invoice-meta');
  const rows: Array<{ label: string; value: string }> = [
    { label: 'Fecha de emisi\u00f3n', value: dateTime(billing.issued_at) },
    ...(billing.customer_name ? [{ label: 'Cliente', value: billing.customer_name }] : []),
    { label: 'Pedido', value: billing.order_id },
    { label: 'Moneda', value: 'Soles (PEN)' }
  ];
  for (const row of rows) {
    meta.append(node('dt', undefined, row.label), node('dd', undefined, row.value));
  }

  const table = node('table', 'invoice-table');
  const headRow = node('tr');
  const columns: Array<{ label: string; numeric: boolean }> = [
    { label: 'Producto', numeric: false },
    { label: 'Cant.', numeric: true },
    { label: 'P. unit.', numeric: true },
    { label: 'Subtotal', numeric: true }
  ];
  for (const column of columns) {
    headRow.append(node('th', column.numeric ? 'num' : undefined, column.label));
  }
  const thead = node('thead');
  thead.append(headRow);
  const tbody = node('tbody');
  for (const item of billing.items) {
    const row = node('tr');
    row.append(
      node('td', undefined, item.name),
      node('td', 'num', String(item.quantity)),
      node('td', 'num', money(item.unit_price_cents)),
      node('td', 'num', money(item.unit_price_cents * item.quantity))
    );
    tbody.append(row);
  }
  table.append(thead, tbody);

  const total = node('div', 'invoice-total');
  total.append(node('span', undefined, 'Total'), node('strong', undefined, money(billing.total_cents)));

  const note = node(
    'p',
    'invoice-note',
    'Comprobante interno de demostraci\u00f3n. No es un comprobante SUNAT ni indica que se haya cobrado.'
  );

  card.append(head, meta, table, total, note);
  return card;
}

function buildContent(orderId: string, entry: Entry): Node[] {
  if (entry.state === 'missing') {
    return [];
  }
  const label = node('h3', 'section-label', 'Facturaci\u00f3n');
  if (entry.state === 'issued' && entry.billing) {
    return [label, invoiceCard(entry.billing)];
  }
  if (entry.state === 'failed') {
    return [
      label,
      warningBox(
        orderId,
        'Facturaci\u00f3n pendiente de reintento',
        'Tu pedido sigue confirmado. El comprobante se emitir\u00e1 cuando el servicio de facturaci\u00f3n est\u00e9 disponible.'
      )
    ];
  }
  if (entry.state === 'stalled') {
    return [
      label,
      warningBox(
        orderId,
        'La facturaci\u00f3n sigue pendiente',
        'Tu pedido sigue confirmado. Puedes consultar de nuevo en unos segundos.'
      )
    ];
  }
  return [label, loadingBox()];
}

function fill(slot: HTMLElement, orderId: string, entry: Entry): void {
  slot.replaceChildren(...buildContent(orderId, entry));
}

function refresh(orderId: string): void {
  const entry = entries.get(orderId);
  if (!entry) {
    return;
  }
  document.querySelectorAll<HTMLElement>('[data-invoice-slot]').forEach((slot) => {
    if (slot.dataset.invoiceSlot === orderId) {
      fill(slot, orderId, entry);
    }
  });
}

function schedule(orderId: string, delay: number): void {
  const entry = entries.get(orderId);
  if (!entry) {
    return;
  }
  if (entry.timer !== null) {
    window.clearTimeout(entry.timer);
  }
  entry.timer = window.setTimeout(() => {
    void tick(orderId);
  }, delay);
}

async function tick(orderId: string): Promise<void> {
  const entry = entries.get(orderId);
  if (!entry) {
    return;
  }
  entry.timer = null;
  entry.attempts += 1;
  entry.controller?.abort();
  const controller = new AbortController();
  entry.controller = controller;

  const result = await fetchBilling(orderId, controller.signal);
  if (entry.controller !== controller) {
    return;
  }
  entry.controller = null;

  if (result.kind === 'ok') {
    entry.billing = result.billing;
    entry.state =
      result.billing.status === 'issued'
        ? 'issued'
        : result.billing.status === 'failed'
          ? 'failed'
          : 'loading';
  } else if (result.kind === 'missing') {
    entry.state = 'missing';
  }

  if (entry.state === 'loading') {
    if (entry.attempts >= maxAttempts) {
      entry.state = 'stalled';
    } else {
      schedule(orderId, pollIntervalMs);
    }
  }
  refresh(orderId);
}

function retry(orderId: string): void {
  const entry = entries.get(orderId);
  if (!entry) {
    return;
  }
  entry.attempts = 0;
  entry.state = 'loading';
  refresh(orderId);
  schedule(orderId, 0);
}

export function invoiceNode(orderId: string): HTMLElement {
  let entry = entries.get(orderId);
  if (!entry) {
    entry = { state: 'loading', billing: null, attempts: 0, timer: null, controller: null };
    entries.set(orderId, entry);
    schedule(orderId, 600);
  }
  const slot = node('div', 'invoice-slot');
  slot.dataset.invoiceSlot = orderId;
  fill(slot, orderId, entry);
  return slot;
}
