import { API_BASE_URL } from "./api";
import type { LedgerEvent, LedgerEventType } from "./types";

export type EventStreamStatus =
  | "idle"
  | "connecting"
  | "open"
  | "reconnecting"
  | "error"
  | "closed";

export interface LedgerEventStreamOptions {
  url?: string;
  fetch?: typeof fetch;
  onEvent: (event: LedgerEvent) => void;
  onStatus?: (status: EventStreamStatus) => void;
  onError?: (error: Error) => void;
  initialLastEventId?: string;
  initialReconnectDelayMs?: number;
  maximumReconnectDelayMs?: number;
  maximumRememberedEventIds?: number;
}

interface SseFrame {
  event: string;
  data: string;
  id: string | undefined;
}

interface StreamAttemptState {
  acceptedAtMs: number | null;
  eventConsumed: boolean;
}

const STABLE_STREAM_DURATION_MS = 15_000;
const MAX_EVENT_ID = "9223372036854775807";
const EVENT_ID_PATTERN = /^(0|[1-9][0-9]{0,18})$/;

const KNOWN_EVENT_TYPES: ReadonlySet<string> = new Set<LedgerEventType>([
  "posting.created",
  "reversal.created",
  "request.replayed",
  "reconciliation.completed",
  "reconciliation.resolved",
]);

function defaultEventUrl(): string {
  const base = API_BASE_URL.endsWith("/")
    ? API_BASE_URL.slice(0, -1)
    : API_BASE_URL;
  return `${base}/events/stream`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function validEventId(value: string, allowZero: boolean): boolean {
  if (!EVENT_ID_PATTERN.test(value) || (!allowZero && value === "0")) return false;
  return value.length < MAX_EVENT_ID.length ||
    (value.length === MAX_EVENT_ID.length && value <= MAX_EVENT_ID);
}

function compareEventIds(left: string, right: string): number {
  if (left.length !== right.length) return left.length - right.length;
  return left === right ? 0 : left < right ? -1 : 1;
}

function parseLedgerEvent(frame: SseFrame): LedgerEvent {
  let data: unknown;
  try {
    data = JSON.parse(frame.data) as unknown;
  } catch (error) {
    throw new EventStreamError("Event data was not valid JSON", null, error);
  }

  if (
    !isRecord(data) ||
    typeof data.id !== "string" ||
    !validEventId(data.id, false) ||
    typeof data.event_type !== "string" ||
    !KNOWN_EVENT_TYPES.has(data.event_type) ||
    typeof data.aggregate_type !== "string" ||
    typeof data.aggregate_id !== "string" ||
    !(typeof data.request_id === "string" || data.request_id === null) ||
    typeof data.created_at !== "string" ||
    !isRecord(data.payload)
  ) {
    throw new EventStreamError("Event data did not match the LedgerLite contract");
  }

  if (frame.event !== "message" && frame.event !== data.event_type) {
    throw new EventStreamError("SSE event name did not match its data envelope");
  }
  if (frame.id !== undefined && frame.id !== data.id) {
    throw new EventStreamError("SSE event ID did not match its data envelope");
  }

  return data as unknown as LedgerEvent;
}

export class EventStreamError extends Error {
  readonly status: number | null;

  constructor(message: string, status: number | null = null, cause?: unknown) {
    super(message, cause === undefined ? undefined : { cause });
    this.name = "EventStreamError";
    this.status = status;
  }
}

function abortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function wait(delayMs: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const timeout = globalThis.setTimeout(resolve, delayMs);
    signal.addEventListener(
      "abort",
      () => {
        globalThis.clearTimeout(timeout);
        resolve();
      },
      { once: true },
    );
  });
}

/**
 * A dependency-free SSE consumer with explicit Last-Event-ID resume support.
 * The backend is at-least-once, so IDs are retained in a bounded dedupe set.
 */
export class LedgerEventStream {
  private readonly url: string;
  private readonly fetchImpl: typeof fetch;
  private readonly onEvent: (event: LedgerEvent) => void;
  private readonly onStatus: ((status: EventStreamStatus) => void) | undefined;
  private readonly onError: ((error: Error) => void) | undefined;
  private readonly initialReconnectDelayMs: number;
  private readonly maximumReconnectDelayMs: number;
  private readonly maximumRememberedEventIds: number;
  private controller: AbortController | null = null;
  private currentStatus: EventStreamStatus = "idle";
  private lastEventId: string | null;
  private readonly rememberedIds = new Set<string>();
  private readonly rememberedIdOrder: string[] = [];
  private serverReconnectDelayMs: number | null = null;

  constructor(options: LedgerEventStreamOptions) {
    this.url = options.url ?? defaultEventUrl();
    this.fetchImpl = options.fetch ?? globalThis.fetch.bind(globalThis);
    this.onEvent = options.onEvent;
    this.onStatus = options.onStatus;
    this.onError = options.onError;
    this.initialReconnectDelayMs = Math.max(
      100,
      options.initialReconnectDelayMs ?? 1_000,
    );
    this.maximumReconnectDelayMs = Math.max(
      this.initialReconnectDelayMs,
      options.maximumReconnectDelayMs ?? 15_000,
    );
    this.maximumRememberedEventIds = Math.max(
      1,
      options.maximumRememberedEventIds ?? 2_048,
    );
    this.lastEventId = options.initialLastEventId ?? null;
    if (this.lastEventId !== null) {
      if (!validEventId(this.lastEventId, true)) {
        throw new RangeError("Initial event ID must be a PostgreSQL bigint cursor");
      }
      if (this.lastEventId !== "0") {
        this.remember(this.lastEventId);
      }
    }
  }

  get status(): EventStreamStatus {
    return this.currentStatus;
  }

  get resumeAfterEventId(): string | null {
    return this.lastEventId;
  }

  start(): void {
    if (this.controller !== null) {
      return;
    }
    this.controller = new AbortController();
    void this.run(this.controller.signal);
  }

  close(): void {
    const controller = this.controller;
    this.controller = null;
    controller?.abort();
    this.setStatus("closed");
  }

  private setStatus(status: EventStreamStatus): void {
    if (this.currentStatus === status) {
      return;
    }
    this.currentStatus = status;
    try {
      this.onStatus?.(status);
    } catch {
      // A rendering callback must not tear down the transport loop.
    }
  }

  private reportError(error: unknown): void {
    const normalized = error instanceof Error
      ? error
      : new EventStreamError("The event stream failed");
    this.setStatus("error");
    try {
      this.onError?.(normalized);
    } catch {
      // Error presentation is intentionally isolated from reconnect behavior.
    }
  }

  private remember(id: string): boolean {
    if (this.rememberedIds.has(id)) {
      return false;
    }
    this.rememberedIds.add(id);
    this.rememberedIdOrder.push(id);
    while (this.rememberedIdOrder.length > this.maximumRememberedEventIds) {
      const oldest = this.rememberedIdOrder.shift();
      if (oldest !== undefined) {
        this.rememberedIds.delete(oldest);
      }
    }
    return true;
  }

  private dispatch(frame: SseFrame): boolean {
    if (frame.data === "") {
      return false;
    }
    try {
      const event = parseLedgerEvent(frame);
      const previousHighWaterMark = this.lastEventId;
      if (
        previousHighWaterMark !== null &&
        compareEventIds(event.id, previousHighWaterMark) <= 0
      ) {
        return false;
      }
      this.lastEventId = event.id;
      if (!this.remember(event.id)) {
        return false;
      }
      this.onEvent(event);
      return true;
    } catch (error) {
      this.reportError(error);
      this.setStatus("open");
      return false;
    }
  }

  private async consume(
    response: Response,
    signal: AbortSignal,
    attempt: StreamAttemptState,
  ): Promise<void> {
    if (response.status === 204) {
      this.close();
      return;
    }
    if (!response.ok) {
      throw new EventStreamError(
        `Event stream request failed with status ${response.status}`,
        response.status,
      );
    }
    const contentType = response.headers.get("Content-Type") ?? "";
    if (!contentType.toLowerCase().startsWith("text/event-stream")) {
      throw new EventStreamError("Event stream returned an unexpected content type");
    }
    if (response.body === null) {
      throw new EventStreamError("Streaming responses are unavailable in this browser");
    }

    this.setStatus("open");
    attempt.acceptedAtMs = globalThis.performance.now();
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffered = "";
    let eventName = "message";
    let eventData = "";
    let eventId: string | undefined;

    const processLine = (rawLine: string): void => {
      const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
      if (line === "") {
        attempt.eventConsumed = this.dispatch({ event: eventName, data: eventData, id: eventId }) || attempt.eventConsumed;
        eventName = "message";
        eventData = "";
        eventId = undefined;
        return;
      }
      if (line.startsWith(":")) {
        return;
      }
      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      let value = colon === -1 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) {
        value = value.slice(1);
      }
      if (field === "data") {
        eventData = eventData === "" ? value : `${eventData}\n${value}`;
      } else if (field === "event") {
        eventName = value;
      } else if (field === "id" && !value.includes("\0")) {
        eventId = value;
      } else if (field === "retry" && /^\d+$/.test(value)) {
        this.serverReconnectDelayMs = Math.min(
          Math.max(Number(value), 100),
          this.maximumReconnectDelayMs,
        );
      }
    };

    try {
      while (!signal.aborted) {
        const { value, done } = await reader.read();
        if (done) {
          buffered += decoder.decode();
          break;
        }
        buffered += decoder.decode(value, { stream: true });
        let newline = buffered.indexOf("\n");
        while (newline !== -1) {
          processLine(buffered.slice(0, newline));
          buffered = buffered.slice(newline + 1);
          newline = buffered.indexOf("\n");
        }
      }
      if (buffered !== "") {
        processLine(buffered);
      }
      if (eventData !== "") {
        attempt.eventConsumed = this.dispatch({ event: eventName, data: eventData, id: eventId }) || attempt.eventConsumed;
      }
    } finally {
      reader.releaseLock();
    }
  }

  private async run(signal: AbortSignal): Promise<void> {
    let reconnectAttempt = 0;
    while (!signal.aborted && this.controller !== null) {
      const attempt: StreamAttemptState = { acceptedAtMs: null, eventConsumed: false };
      this.setStatus(reconnectAttempt === 0 ? "connecting" : "reconnecting");
      const headers = new Headers({ Accept: "text/event-stream" });
      if (this.lastEventId !== null) {
        headers.set("Last-Event-ID", String(this.lastEventId));
      }

      try {
        const response = await this.fetchImpl(this.url, {
          method: "GET",
          headers,
          credentials: "same-origin",
          cache: "no-store",
          signal,
        });
        await this.consume(response, signal, attempt);
      } catch (error) {
        if (signal.aborted || abortError(error)) {
          break;
        }
        this.reportError(error);
      }

      if (signal.aborted || this.controller === null) {
        break;
      }
      const stableDuration = attempt.acceptedAtMs !== null &&
        globalThis.performance.now() - attempt.acceptedAtMs >= STABLE_STREAM_DURATION_MS;
      if (attempt.eventConsumed || stableDuration) {
        reconnectAttempt = 0;
      }
      reconnectAttempt += 1;
      this.setStatus("reconnecting");
      const exponentialDelay = Math.min(
        this.initialReconnectDelayMs * 2 ** Math.min(reconnectAttempt - 1, 8),
        this.maximumReconnectDelayMs,
      );
      await wait(this.serverReconnectDelayMs ?? exponentialDelay, signal);
    }
  }
}

export function connectLedgerEvents(
  options: LedgerEventStreamOptions,
): LedgerEventStream {
  const stream = new LedgerEventStream(options);
  stream.start();
  return stream;
}
