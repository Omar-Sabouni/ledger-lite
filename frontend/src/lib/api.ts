import type {
  Account,
  AccountCreateRequest,
  AccountListParams,
  AccountsPage,
  ApiResult,
  ConsoleCapabilities,
  DepositRequest,
  DepositResponse,
  HealthResponse,
  OverviewResponse,
  PageParams,
  ProblemDetails,
  ReconciliationIgnoreRequest,
  ReconciliationItem,
  ReconciliationItemListParams,
  ReconciliationItemsPage,
  ReconciliationMatchRequest,
  ReconciliationRun,
  ReconciliationRunsPage,
  ReversalRequest,
  ReversalResponse,
  StatementPage,
  TransactionDetail,
  TransactionListParams,
  TransactionsPage,
  TransferRequest,
  TransferResponse,
  UUID,
} from "./types";

declare global {
  interface ImportMetaEnv {
    readonly VITE_API_BASE_URL?: string;
  }

  interface ImportMeta {
    readonly env: ImportMetaEnv;
  }
}

function normalizeBaseUrl(baseUrl: string): string {
  const trimmed = baseUrl.trim();
  if (trimmed === "") {
    return "/api/v1";
  }
  return trimmed.endsWith("/") ? trimmed.slice(0, -1) : trimmed;
}

export const API_BASE_URL = normalizeBaseUrl(
  import.meta.env?.VITE_API_BASE_URL ?? "/api/v1",
);

function serviceRootFromApiBase(apiBaseUrl: string): string {
  if (apiBaseUrl.endsWith("/api/v1")) {
    return apiBaseUrl.slice(0, -"/api/v1".length);
  }
  return apiBaseUrl;
}

export const SERVICE_BASE_URL = serviceRootFromApiBase(API_BASE_URL);

type QueryValue = string | number | boolean | null | undefined;

function withQuery(
  path: string,
  params: Readonly<Record<string, QueryValue>>,
): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      search.set(key, String(value));
    }
  }
  const query = search.toString();
  return query === "" ? path : `${path}?${query}`;
}

function joinUrl(baseUrl: string, path: string): string {
  if (/^https?:\/\//i.test(path)) {
    return path;
  }
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `${baseUrl}${normalizedPath}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringField(
  value: Record<string, unknown>,
  field: string,
  fallback: string,
): string {
  const candidate = value[field];
  return typeof candidate === "string" ? candidate : fallback;
}

function numberField(
  value: Record<string, unknown>,
  field: string,
  fallback: number,
): number {
  const candidate = value[field];
  return typeof candidate === "number" && Number.isFinite(candidate)
    ? candidate
    : fallback;
}

async function readJson(response: Response): Promise<unknown> {
  const text = await response.text();
  if (text === "") {
    return null;
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new ApiError({
      type: "about:blank",
      title: "Invalid server response",
      status: response.status,
      detail: "LedgerLite returned a response that was not valid JSON.",
      code: "invalid_response",
      instance: new URL(response.url, globalThis.location?.href).pathname,
      request_id: response.headers.get("X-Request-ID") ?? "unavailable",
    });
  }
}

function toProblemDetails(response: Response, body: unknown): ProblemDetails {
  const fallbackRequestId = response.headers.get("X-Request-ID") ?? "unavailable";
  const fallbackInstance = (() => {
    try {
      return new URL(response.url, globalThis.location?.href).pathname;
    } catch {
      return "";
    }
  })();

  if (!isRecord(body)) {
    return {
      type: "about:blank",
      title: response.statusText || "Request failed",
      status: response.status,
      detail: "LedgerLite could not complete the request.",
      code: "http_error",
      instance: fallbackInstance,
      request_id: fallbackRequestId,
    };
  }

  return {
    type: stringField(body, "type", "about:blank"),
    title: stringField(body, "title", response.statusText || "Request failed"),
    status: numberField(body, "status", response.status),
    detail: stringField(body, "detail", "LedgerLite could not complete the request."),
    code: stringField(body, "code", "http_error"),
    instance: stringField(body, "instance", fallbackInstance),
    request_id: stringField(body, "request_id", fallbackRequestId),
  };
}

export function isProblemDetails(value: unknown): value is ProblemDetails {
  if (!isRecord(value)) {
    return false;
  }
  return (
    typeof value.type === "string" &&
    typeof value.title === "string" &&
    typeof value.status === "number" &&
    typeof value.detail === "string" &&
    typeof value.code === "string" &&
    typeof value.instance === "string" &&
    typeof value.request_id === "string"
  );
}

export class ApiError extends Error {
  readonly problem: ProblemDetails;
  readonly status: number;
  readonly code: string;
  readonly requestId: string;

  constructor(problem: ProblemDetails, options?: ErrorOptions) {
    super(problem.detail, options);
    this.name = "ApiError";
    this.problem = problem;
    this.status = problem.status;
    this.code = problem.code;
    this.requestId = problem.request_id;
  }
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

function clientProblem(
  code: "network_error" | "request_aborted",
  detail: string,
): ProblemDetails {
  return {
    type: "about:blank",
    title: code === "request_aborted" ? "Request cancelled" : "Connection unavailable",
    status: 0,
    detail,
    code,
    instance: "",
    request_id: "unavailable",
  };
}

function validateIdempotencyKey(key: string): void {
  if (key.length < 1 || key.length > 255 || !/^[!-~]+$/.test(key)) {
    throw new TypeError(
      "Idempotency key must contain 1–255 visible ASCII characters",
    );
  }
}

export function createIdempotencyKey(operation = "console"): string {
  if (!/^[A-Za-z0-9._-]{1,40}$/.test(operation)) {
    throw new TypeError("Idempotency key prefix contains unsupported characters");
  }
  if (typeof globalThis.crypto?.randomUUID !== "function") {
    throw new Error("This browser cannot generate secure idempotency keys");
  }
  return `${operation}-${globalThis.crypto.randomUUID()}`;
}

interface JsonRequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  headers?: HeadersInit;
  signal?: AbortSignal | undefined;
}

export interface LedgerApiClientOptions {
  baseUrl?: string;
  fetch?: typeof fetch;
}

export class LedgerApiClient {
  readonly baseUrl: string;
  readonly serviceBaseUrl: string;
  private readonly fetchImpl: typeof fetch;

  constructor(options: LedgerApiClientOptions = {}) {
    this.baseUrl = normalizeBaseUrl(options.baseUrl ?? API_BASE_URL);
    this.serviceBaseUrl = serviceRootFromApiBase(this.baseUrl);
    this.fetchImpl = options.fetch ?? globalThis.fetch.bind(globalThis);
  }

  async request<T>(
    path: string,
    options: JsonRequestOptions = {},
  ): Promise<ApiResult<T>> {
    return this.requestUrl<T>(joinUrl(this.baseUrl, path), options);
  }

  private async requestService<T>(
    path: string,
    options: JsonRequestOptions = {},
  ): Promise<ApiResult<T>> {
    return this.requestUrl<T>(joinUrl(this.serviceBaseUrl, path), options);
  }

  private async requestUrl<T>(
    url: string,
    options: JsonRequestOptions,
  ): Promise<ApiResult<T>> {
    const headers = new Headers(options.headers);
    headers.set("Accept", "application/json, application/problem+json");
    if (options.body !== undefined) {
      headers.set("Content-Type", "application/json");
    }

    let response: Response;
    try {
      response = await this.fetchImpl(url, {
        method: options.method ?? "GET",
        headers,
        credentials: "same-origin",
        cache: "no-store",
        ...(options.body === undefined
          ? {}
          : { body: JSON.stringify(options.body) }),
        ...(options.signal === undefined ? {} : { signal: options.signal }),
      });
    } catch (error) {
      if (error instanceof ApiError) {
        throw error;
      }
      if (error instanceof DOMException && error.name === "AbortError") {
        throw new ApiError(
          clientProblem("request_aborted", "The request was cancelled."),
          { cause: error },
        );
      }
      throw new ApiError(
        clientProblem(
          "network_error",
          "LedgerLite is unreachable. Check that the local stack is running.",
        ),
        { cause: error },
      );
    }

    const body = response.status === 204 ? null : await readJson(response);
    if (!response.ok) {
      throw new ApiError(toProblemDetails(response, body));
    }

    return {
      data: body as T,
      status: response.status,
      requestId: response.headers.get("X-Request-ID"),
      idempotentReplayed:
        response.headers.get("Idempotent-Replayed")?.toLowerCase() === "true",
    };
  }

  async overview(signal?: AbortSignal): Promise<OverviewResponse> {
    return (await this.request<OverviewResponse>("/overview", { signal })).data;
  }

  async listAccounts(
    params: AccountListParams = {},
    signal?: AbortSignal,
  ): Promise<AccountsPage> {
    const path = withQuery("/accounts", {
      currency: params.currency,
      limit: params.limit,
      cursor: params.cursor,
    });
    return (await this.request<AccountsPage>(path, { signal })).data;
  }

  async createAccount(
    input: AccountCreateRequest,
    idempotencyKey: string,
    signal?: AbortSignal,
  ): Promise<Account> {
    validateIdempotencyKey(idempotencyKey);
    return (
      await this.request<Account>("/accounts", {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
        body: input,
        signal,
      })
    ).data;
  }

  async deposit(
    accountId: UUID,
    input: DepositRequest,
    idempotencyKey: string,
    signal?: AbortSignal,
  ): Promise<ApiResult<DepositResponse>> {
    validateIdempotencyKey(idempotencyKey);
    return this.request<DepositResponse>(
      `/accounts/${encodeURIComponent(accountId)}/deposits`,
      {
        method: "POST",
        body: input,
        headers: { "Idempotency-Key": idempotencyKey },
        signal,
      },
    );
  }

  async statement(
    accountId: UUID,
    params: PageParams = {},
    signal?: AbortSignal,
  ): Promise<StatementPage> {
    const path = withQuery(
      `/accounts/${encodeURIComponent(accountId)}/statement`,
      { limit: params.limit, cursor: params.cursor },
    );
    return (await this.request<StatementPage>(path, { signal })).data;
  }

  async listTransactions(
    params: TransactionListParams = {},
    signal?: AbortSignal,
  ): Promise<TransactionsPage> {
    const path = withQuery("/transactions", {
      currency: params.currency,
      type: params.type,
      account_id: params.account_id,
      date_from: params.date_from,
      date_to: params.date_to,
      limit: params.limit,
      cursor: params.cursor,
    });
    return (await this.request<TransactionsPage>(path, { signal })).data;
  }

  async transaction(
    transactionId: UUID,
    signal?: AbortSignal,
  ): Promise<TransactionDetail> {
    return (
      await this.request<TransactionDetail>(
        `/transactions/${encodeURIComponent(transactionId)}`,
        { signal },
      )
    ).data;
  }

  async transfer(
    input: TransferRequest,
    idempotencyKey: string,
    signal?: AbortSignal,
  ): Promise<ApiResult<TransferResponse>> {
    validateIdempotencyKey(idempotencyKey);
    return this.request<TransferResponse>("/transfers", {
      method: "POST",
      body: input,
      headers: { "Idempotency-Key": idempotencyKey },
      signal,
    });
  }

  async reverseTransaction(
    transactionId: UUID,
    input: ReversalRequest,
    idempotencyKey: string,
    signal?: AbortSignal,
  ): Promise<ApiResult<ReversalResponse>> {
    validateIdempotencyKey(idempotencyKey);
    return this.request<ReversalResponse>(
      `/transactions/${encodeURIComponent(transactionId)}/reversals`,
      {
        method: "POST",
        body: input,
        headers: { "Idempotency-Key": idempotencyKey },
        signal,
      },
    );
  }

  async listReconciliationRuns(
    signal?: AbortSignal,
  ): Promise<ReconciliationRunsPage> {
    return (
      await this.request<ReconciliationRunsPage>("/reconciliation/runs", {
        signal,
      })
    ).data;
  }

  async reconciliationRun(
    runId: UUID,
    signal?: AbortSignal,
  ): Promise<ReconciliationRun> {
    return (
      await this.request<ReconciliationRun>(
        `/reconciliation/runs/${encodeURIComponent(runId)}`,
        { signal },
      )
    ).data;
  }

  async executeReconciliation(
    runId: UUID,
    signal?: AbortSignal,
  ): Promise<ReconciliationRun> {
    return (
      await this.request<ReconciliationRun>(
        `/reconciliation/runs/${encodeURIComponent(runId)}/execute`,
        { method: "POST", signal },
      )
    ).data;
  }

  async listReconciliationItems(
    runId: UUID,
    params: ReconciliationItemListParams = {},
    signal?: AbortSignal,
  ): Promise<ReconciliationItemsPage> {
    const path = withQuery(
      `/reconciliation/runs/${encodeURIComponent(runId)}/items`,
      {
        result: params.result,
        resolution_status: params.resolution_status,
        limit: params.limit,
        cursor: params.cursor,
      },
    );
    return (await this.request<ReconciliationItemsPage>(path, { signal })).data;
  }

  async matchReconciliationItem(
    itemId: UUID,
    input: ReconciliationMatchRequest,
    signal?: AbortSignal,
  ): Promise<ReconciliationItem> {
    return (
      await this.request<ReconciliationItem>(
        `/reconciliation/items/${encodeURIComponent(itemId)}/match`,
        { method: "POST", body: input, signal },
      )
    ).data;
  }

  async ignoreReconciliationItem(
    itemId: UUID,
    input: ReconciliationIgnoreRequest,
    signal?: AbortSignal,
  ): Promise<ReconciliationItem> {
    return (
      await this.request<ReconciliationItem>(
        `/reconciliation/items/${encodeURIComponent(itemId)}/ignore`,
        { method: "POST", body: input, signal },
      )
    ).data;
  }

  async liveness(signal?: AbortSignal): Promise<HealthResponse> {
    return (
      await this.requestService<HealthResponse>("/livez", { signal })
    ).data;
  }

  async capabilities(signal?: AbortSignal): Promise<ConsoleCapabilities> {
    return (
      await this.request<ConsoleCapabilities>("/capabilities", { signal })
    ).data;
  }

  async readiness(signal?: AbortSignal): Promise<HealthResponse> {
    return (
      await this.requestService<HealthResponse>("/readyz", { signal })
    ).data;
  }
}

export function createApiClient(
  options: LedgerApiClientOptions = {},
): LedgerApiClient {
  return new LedgerApiClient(options);
}

export const api = createApiClient();
