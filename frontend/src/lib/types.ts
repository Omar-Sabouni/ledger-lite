/**
 * The public LedgerLite wire contract.
 *
 * Money deliberately remains a string at the JSON boundary. Converting it to
 * a JavaScript number would make values above Number.MAX_SAFE_INTEGER lossy.
 */
export type UUID = string;
export type CurrencyCode = string;
export type MoneyString = string;
export type SignedMoneyString = string;
export type IsoTimestamp = string;
export type Cursor = string;

export type TransactionType = "deposit" | "transfer" | "reversal";
export type ReversalReasonCode =
  | "duplicate"
  | "customer_request"
  | "operator_correction"
  | "other";

export interface ProblemDetails {
  type: string;
  title: string;
  status: number;
  detail: string;
  code: string;
  instance: string;
  request_id: string;
}

export interface HealthResponse {
  status: "ok";
}

export interface ConsoleCapabilities {
  documentation: boolean;
}

export interface OverviewCurrency {
  currency: CurrencyCode;
  customer_accounts: number;
  total_customer_funds: SignedMoneyString;
  clearing_balance: SignedMoneyString;
  net_imbalance: SignedMoneyString;
}

export interface IntegritySummary {
  transaction_count: number;
  entry_count: number;
  reversal_count: number;
  unbalanced_transaction_count: number;
  replay_count: number;
  open_reconciliation_exceptions: number;
}

export interface OverviewResponse {
  as_of: IsoTimestamp;
  currencies: OverviewCurrency[];
  integrity: IntegritySummary;
}

export interface Account {
  id: UUID;
  display_name: string | null;
  currency: CurrencyCode;
  balance: SignedMoneyString;
  created_at: IsoTimestamp;
}

export interface AccountsPage {
  items: Account[];
  next_cursor: Cursor | null;
}

export interface AccountCreateRequest {
  currency: CurrencyCode;
  display_name?: string | null | undefined;
}

export interface DepositRequest {
  amount: MoneyString;
}

export interface DepositResponse {
  transaction_id: UUID;
  account_id: UUID;
  amount: MoneyString;
  currency: CurrencyCode;
  balance: SignedMoneyString;
  created_at: IsoTimestamp;
}

export interface TransferRequest {
  source_account_id: UUID;
  destination_account_id: UUID;
  amount: MoneyString;
}

export interface TransferResponse {
  transaction_id: UUID;
  source_account_id: UUID;
  destination_account_id: UUID;
  amount: MoneyString;
  currency: CurrencyCode;
  created_at: IsoTimestamp;
}

export interface StatementEntry {
  id: UUID;
  transaction_id: UUID;
  type: TransactionType;
  amount: SignedMoneyString;
  currency: CurrencyCode;
  counterparty_account_id: UUID | null;
  created_at: IsoTimestamp;
  balance_after: SignedMoneyString;
}

export interface StatementPage {
  account: Account;
  balance: SignedMoneyString;
  items: StatementEntry[];
  next_cursor: Cursor | null;
}

export interface TransactionSummary {
  id: UUID;
  type: TransactionType;
  amount: MoneyString;
  currency: CurrencyCode;
  source_account_id: UUID;
  destination_account_id: UUID;
  source_display_name: string | null;
  destination_display_name: string | null;
  created_at: IsoTimestamp;
  reverses_transaction_id: UUID | null;
  reversed_by_transaction_id: UUID | null;
  reversal_reason_code: ReversalReasonCode | null;
  reversal_note: string | null;
}

export interface TransactionsPage {
  items: TransactionSummary[];
  next_cursor: Cursor | null;
}

export interface PostingEntry {
  id: UUID;
  sequence: 1 | 2;
  account_id: UUID;
  account_display_name: string | null;
  amount: SignedMoneyString;
  currency: CurrencyCode;
  created_at: IsoTimestamp;
}

export interface TransactionIntegrity {
  entry_count: number;
  posting_sum: SignedMoneyString;
  balanced: boolean;
  currency_consistent: boolean;
}

export interface TransactionDetail extends TransactionSummary {
  entries: PostingEntry[];
  integrity: TransactionIntegrity;
}

export interface ReversalRequest {
  reason_code: ReversalReasonCode;
  note?: string | undefined;
}

export interface ReversalResponse {
  transaction_id: UUID;
  reverses_transaction_id: UUID;
  source_account_id: UUID;
  destination_account_id: UUID;
  amount: MoneyString;
  currency: CurrencyCode;
  reason_code: ReversalReasonCode;
  note: string | null;
  created_at: IsoTimestamp;
}

export type ReconciliationRunStatus = "pending" | "completed";
export type ReconciliationItemResult =
  | "pending"
  | "matched"
  | "provider_only"
  | "ledger_only"
  | "duplicate"
  | "mismatched";
export type ReconciliationResolution = "open" | "matched" | "ignored";
export type ReconciliationMismatchCode =
  | "transaction_not_found"
  | "amount_mismatch"
  | "currency_mismatch"
  | "transaction_type_mismatch"
  | "outside_period"
  | "duplicate_claim"
  | "unclaimed_ledger_transaction";

export interface ReconciliationCounts {
  matched: number;
  provider_only: number;
  ledger_only: number;
  mismatched: number;
  duplicate: number;
  open_exceptions: number;
}

export interface ReconciliationGrossVolume {
  currency: CurrencyCode;
  provider_total: SignedMoneyString;
  ledger_total: SignedMoneyString;
  difference: SignedMoneyString;
}

export interface ReconciliationSummary {
  counts: ReconciliationCounts;
  gross_volume: ReconciliationGrossVolume;
}

/** Summary fields shared by reconciliation list and detail responses. */
export interface ReconciliationRun {
  id: UUID;
  provider: string;
  currency: CurrencyCode;
  period_start: IsoTimestamp;
  period_end: IsoTimestamp;
  status: ReconciliationRunStatus;
  summary: ReconciliationSummary | null;
  created_at: IsoTimestamp;
  completed_at: IsoTimestamp | null;
}

export interface ReconciliationRunsPage {
  items: ReconciliationRun[];
}

export interface ReconciliationItem {
  id: UUID;
  run_id: UUID;
  provider_reference: string | null;
  claimed_transaction_id: UUID | null;
  matched_transaction_id: UUID | null;
  amount: MoneyString;
  currency: CurrencyCode;
  occurred_at: IsoTimestamp;
  result: ReconciliationItemResult;
  mismatch_code: ReconciliationMismatchCode | null;
  resolution_status: ReconciliationResolution;
  resolution_note: string | null;
  created_at: IsoTimestamp;
  resolved_at: IsoTimestamp | null;
}

export interface ReconciliationItemsPage {
  items: ReconciliationItem[];
  next_cursor: Cursor | null;
}

export interface ReconciliationMatchRequest {
  transaction_id: UUID;
  note?: string | undefined;
}

export interface ReconciliationIgnoreRequest {
  reason: string;
}

export type LedgerEventType =
  | "posting.created"
  | "reversal.created"
  | "request.replayed"
  | "reconciliation.completed"
  | "reconciliation.resolved";

export interface LedgerEvent {
  id: string;
  event_type: LedgerEventType;
  aggregate_type: string;
  aggregate_id: UUID;
  request_id: string | null;
  created_at: IsoTimestamp;
  payload: Record<string, unknown>;
}

export interface PageParams {
  limit?: number | undefined;
  cursor?: Cursor | undefined;
}

export interface AccountListParams extends PageParams {
  currency?: CurrencyCode | undefined;
}

export interface TransactionListParams extends PageParams {
  currency?: CurrencyCode | undefined;
  type?: TransactionType | undefined;
  account_id?: UUID | undefined;
  date_from?: IsoTimestamp | undefined;
  date_to?: IsoTimestamp | undefined;
}

export interface ReconciliationItemListParams extends PageParams {
  result?: ReconciliationItemResult | undefined;
  resolution_status?: ReconciliationResolution | undefined;
}

export interface ApiResult<T> {
  data: T;
  status: number;
  requestId: string | null;
  idempotentReplayed: boolean;
}
