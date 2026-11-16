interface TraceStep {
  table: string;
  op: string;
  sql: string;
  note: string;
  done: boolean;
  at: string | null;
}

interface TraceGroup {
  id: string;
  title: string;
  actor: string;
  steps: TraceStep[];
}

interface Trace {
  order_id: string;
  complete: boolean;
  groups: TraceGroup[];
}

type TraceState = 'loading' | 'ready' | 'stalled' | 'missing';

interface Entry {
  state: TraceState;
  trace: Trace | null;
  attempts: number;
  open: boolean;
  timer: number | null;
  controller: AbortController | null;
}

type FetchResult = { kind: 'ok'; trace: Trace } | { kind: 'missing' } | { kind: 'error' };

const entries = new Map<string, Entry>();
const pollIntervalMs = 1500;
const maxAttempts = 30;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function parseStep(value: unknown): TraceStep | null {
  if (
    !isRecord(value) ||
    typeof value.table !== 'string' ||
    typeof value.op !== 'string' ||
    typeof value.sql !== 'string' ||
    typeof value.note !== 'string' ||
    typeof value.done !== 'boolean'
  ) {
    return null;
  }
  return {
    table: value.table,
    op: value.op,
    sql: value.sql,
    note: value.note,
    done: value.done,
    at: typeof value.at === 'string' ? value.at : null
  };
}

function parseTrace(value: unknown): Trace | null {
  if (!isRecord(value) || !Array.isArray(value.groups) || typeof value.complete !== 'boolean') {
    return null;
  }
  const groups: TraceGroup[] = [];
  for (const rawGroup of value.groups) {
    if (
      !isRecord(rawGroup) ||
      typeof rawGroup.id !== 'string' ||
      typeof rawGroup.title !== 'string' ||
      typeof rawGroup.actor !== 'string' ||
      !Array.isArray(rawGroup.steps)
    ) {
      return null;
    }
    const steps: TraceStep[] = [];
    for (const rawStep of rawGroup.steps) {
      const step = parseStep(rawStep);
      if (step === null) {
        return null;
      }
      steps.push(step);
    }
    groups.push({ id: rawGroup.id, title: rawGroup.title, actor: rawGroup.actor, steps });
  }
  return {
    order_id: typeof value.order_id === 'string' ? value.order_id : '',
    complete: value.complete,
    groups
  };
}

async function fetchTrace(orderId: string, signal: AbortSignal): Promise<FetchResult> {
  try {
    const response = await fetch(`/api/v1/orders/${encodeURIComponent(orderId)}/trace`, {
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
    const trace = parseTrace(await response.json());
    return trace ? { kind: 'ok', trace } : { kind: 'error' };
  } catch {
    return { kind: 'error' };
  }
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

function pad(value: number, width = 2): string {
  return String(value).padStart(width, '0');
}

function clock(iso: string | null): string {
  if (!iso) {
    return 'pendiente';
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return 'pendiente';
  }
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(date.getMilliseconds(), 3)}`;
}

function stepNode(step: TraceStep): HTMLElement {
  const item = node('li', `trace-step ${step.done ? 'trace-step-done' : 'trace-step-wait'}`);
  const mark = node('span', 'trace-mark', step.done ? '\u2713' : '\u2026');
  mark.setAttribute('aria-hidden', 'true');

  const head = node('div', 'trace-head');
  const opClass = step.op.toLowerCase().replace(/[^a-z]/g, '');
  head.append(
    node('span', 'trace-table', step.table),
    node('span', `trace-op trace-op-${opClass}`, step.op),
    node('span', 'trace-time', clock(step.at))
  );

  const main = node('div', 'trace-main');
  main.append(head, node('p', 'trace-note', step.note), node('code', 'trace-sql', step.sql));

  const state = node('span', 'trace-sr', step.done ? 'Completado' : 'Pendiente');
  item.append(mark, main, state);
  return item;
}

function groupNode(group: TraceGroup): HTMLElement {
  const section = node('section', 'trace-group');
  section.append(node('h4', 'trace-group-title', group.title), node('p', 'trace-actor', group.actor));
  const list = node('ol', 'trace-steps');
  for (const step of group.steps) {
    list.append(stepNode(step));
  }
  section.append(list);
  return section;
}

function buildContent(orderId: string, entry: Entry): Node[] {
  if (entry.state === 'missing') {
    return [];
  }
  const details = node('details', 'trace');
  details.open = entry.open;
  details.addEventListener('toggle', () => {
    entry.open = details.open;
  });
  details.append(node('summary', 'trace-summary', '\u00bfQu\u00e9 pas\u00f3 en la base de datos?'));

  if (entry.trace === null) {
    details.append(node('p', 'trace-intro', 'Consultando las tablas del pedido\u2026'));
    return [details];
  }

  details.append(
    node(
      'p',
      'trace-intro',
      'Esto es lo que qued\u00f3 escrito de verdad en PostgreSQL para este pedido, en el orden en que ocurri\u00f3. El SQL mostrado es equivalente y de ilustraci\u00f3n.'
    )
  );
  for (const group of entry.trace.groups) {
    details.append(groupNode(group));
  }

  const footer = node('div', 'trace-footer');
  const status = entry.trace.complete
    ? 'Todos los pasos terminaron.'
    : entry.state === 'stalled'
      ? 'Algunos pasos siguen pendientes.'
      : 'Actualizando\u2026';
  footer.append(node('span', 'trace-status', status));
  const button = node('button', 'button button-secondary', 'Actualizar');
  button.type = 'button';
  button.addEventListener('click', () => {
    restart(orderId);
  });
  footer.append(button);
  details.append(footer);
  return [details];
}

function fill(slot: HTMLElement, orderId: string, entry: Entry): void {
  slot.replaceChildren(...buildContent(orderId, entry));
}

function refresh(orderId: string): void {
  const entry = entries.get(orderId);
  if (!entry) {
    return;
  }
  document.querySelectorAll<HTMLElement>('[data-trace-slot]').forEach((slot) => {
    if (slot.dataset.traceSlot === orderId) {
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

  const result = await fetchTrace(orderId, controller.signal);
  if (entry.controller !== controller) {
    return;
  }
  entry.controller = null;

  if (result.kind === 'ok') {
    entry.trace = result.trace;
    entry.state = 'ready';
  } else if (result.kind === 'missing') {
    entry.state = 'missing';
  }

  const finished = entry.trace !== null && entry.trace.complete;
  if (entry.state !== 'missing' && !finished) {
    if (entry.attempts >= maxAttempts) {
      entry.state = 'stalled';
    } else {
      schedule(orderId, pollIntervalMs);
    }
  }
  refresh(orderId);
}

function restart(orderId: string): void {
  const entry = entries.get(orderId);
  if (!entry) {
    return;
  }
  entry.attempts = 0;
  entry.state = entry.trace === null ? 'loading' : 'ready';
  refresh(orderId);
  schedule(orderId, 0);
}

export function traceNode(orderId: string): HTMLElement {
  let entry = entries.get(orderId);
  if (!entry) {
    entry = { state: 'loading', trace: null, attempts: 0, open: true, timer: null, controller: null };
    entries.set(orderId, entry);
    schedule(orderId, 400);
  }
  const slot = node('div', 'trace-slot');
  slot.dataset.traceSlot = orderId;
  fill(slot, orderId, entry);
  return slot;
}
