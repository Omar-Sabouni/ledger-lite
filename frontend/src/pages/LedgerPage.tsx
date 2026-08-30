import { useCallback, useEffect, useMemo, useState, type KeyboardEvent } from "react";

import { Dialog } from "../components/Dialog";
import { Button, Card, EmptyState, ErrorState, PaginationError, SectionHeading, Skeleton, StatusPill, shortId } from "../components/Ui";
import { useAsyncData } from "../hooks/useAsyncData";
import { api, createIdempotencyKey, isApiError } from "../lib/api";
import { formatMoney, parseMoney } from "../lib/money";
import { formatDubaiDate, formatDubaiDateTime, formatUtcDateTime } from "../lib/time";
import type {
  Account,
  AccountsPage,
  ReversalReasonCode,
  StatementPage,
  TransactionDetail,
  TransactionSummary,
  TransactionType,
  TransactionsPage
} from "../lib/types";

type LedgerTab = "transactions" | "accounts";
type PeriodFilter = "all" | "7d" | "30d";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function errorDescription(error: unknown, fallback: string) {
  return isApiError(error) ? error.problem.detail : fallback;
}

function accountLabel(name: string | null, id: string) {
  return name?.trim() || shortId(id);
}

function periodStart(period: PeriodFilter): string | undefined {
  if (period === "all") return undefined;
  const days = period === "7d" ? 7 : 30;
  return new Date(Date.now() - days * 86_400_000).toISOString();
}

function TransactionTable({ items, onInspect }: { items: TransactionSummary[]; onInspect: (id: string) => void }) {
  if (items.length === 0) {
    return <EmptyState title="No transactions found" description="No ledger transactions match these filters. Clear a filter or add transactions." />;
  }
  return (
    <div className="table-scroller" tabIndex={0} aria-label="Ledger transactions">
      <table className="data-table">
        <caption className="sr-only">Ledger transactions matching the active filters</caption>
        <thead><tr><th scope="col">Transaction</th><th scope="col">Posted</th><th scope="col">Type</th><th scope="col">From → To</th><th scope="col">Amount</th><th scope="col">State</th></tr></thead>
        <tbody>
          {items.map((transaction) => (
            <tr key={transaction.id}>
              <td><button className="data-table__action mono" type="button" onClick={() => onInspect(transaction.id)} aria-label={`Inspect transaction ${transaction.id}`}>{shortId(transaction.id)}</button></td>
              <td>{formatDubaiDateTime(transaction.created_at)}</td>
              <td><span className={`kind-badge kind-badge--${transaction.type}`}>{transaction.type}</span></td>
              <td><span className="account-route"><span>{accountLabel(transaction.source_display_name, transaction.source_account_id)}</span><span className="account-route__arrow" aria-hidden="true">→</span><span>{accountLabel(transaction.destination_display_name, transaction.destination_account_id)}</span></span></td>
              <td className="money">{formatMoney(transaction.amount, transaction.currency)}</td>
              <td><StatusPill tone="positive">Balanced</StatusPill></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AccountsTable({ items, onStatement }: { items: Account[]; onStatement: (account: Account) => void }) {
  if (items.length === 0) {
    return <EmptyState title="No accounts found" description="No customer accounts match this currency filter." />;
  }
  return (
    <div className="table-scroller" tabIndex={0} aria-label="Customer accounts">
      <table className="data-table">
        <caption className="sr-only">Customer ledger accounts</caption>
        <thead><tr><th scope="col">Account</th><th scope="col">Currency</th><th scope="col">Calculated balance</th><th scope="col">Opened</th><th scope="col"><span className="sr-only">Action</span></th></tr></thead>
        <tbody>
          {items.map((account) => (
            <tr key={account.id}>
              <td><strong>{account.display_name ?? "Untitled customer account"}</strong><br /><span className="mono subtle">{shortId(account.id)}</span></td>
              <td><StatusPill tone="info">{account.currency}</StatusPill></td>
              <td className={`money ${parseMoney(account.balance) < 0n ? "money--negative" : ""}`}>{formatMoney(account.balance, account.currency)}</td>
              <td>{formatDubaiDate(account.created_at)}</td>
              <td><Button variant="quiet" onClick={() => onStatement(account)}>View statement</Button></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PostingBreakdown({ transaction }: { transaction: TransactionDetail }) {
  const entries = [...transaction.entries].sort((left, right) => left.sequence - right.sequence);
  return (
    <section className="journal-breakdown" aria-labelledby="posting-equation-title">
      <div className="journal-breakdown__header">
        <h3 id="posting-equation-title">Posting equation</h3>
        <StatusPill tone={transaction.integrity.balanced ? "positive" : "critical"}>{transaction.integrity.balanced ? "Balanced" : "Imbalanced"}</StatusPill>
      </div>
      <div className="table-scroller" tabIndex={0} aria-label="Signed journal postings">
        <table className="data-table posting-table">
          <thead><tr><th scope="col">Seq.</th><th scope="col">Account</th><th scope="col">Account ID</th><th scope="col">Signed entry</th></tr></thead>
          <tbody>
            {entries.map((entry) => (
              <tr key={entry.id}>
                <td className="mono">{String(entry.sequence).padStart(2, "0")}</td>
                <td><strong>{accountLabel(entry.account_display_name, entry.account_id)}</strong></td>
                <td className="mono">{shortId(entry.account_id)}</td>
                <td className={`money ${parseMoney(entry.amount) < 0n ? "money--negative" : "money--positive"}`}>{formatMoney(entry.amount, entry.currency, { showPlus: true })}</td>
              </tr>
            ))}
          </tbody>
          <tfoot><tr><th scope="row" colSpan={3}>Posting sum</th><td className="money"><strong>{formatMoney(transaction.integrity.posting_sum, transaction.currency)}</strong></td></tr></tfoot>
        </table>
      </div>
      <dl className="posting-summary">
        <div><dt>Entries</dt><dd><StatusPill tone={transaction.integrity.entry_count === 2 ? "positive" : "critical"}>{transaction.integrity.entry_count}</StatusPill></dd></div>
        <div><dt>Posting sum</dt><dd><StatusPill tone={transaction.integrity.balanced ? "positive" : "critical"}>{transaction.integrity.balanced ? "BALANCED" : "IMBALANCED"}</StatusPill></dd></div>
        <div><dt>Currency</dt><dd><StatusPill tone={transaction.integrity.currency_consistent ? "positive" : "critical"}>{transaction.integrity.currency_consistent ? "CONSISTENT" : "MIXED"}</StatusPill></dd></div>
        <div><dt>Transaction</dt><dd><StatusPill tone="info">COMPLETE</StatusPill></dd></div>
      </dl>
    </section>
  );
}

function TransactionInspector({
  open,
  transaction,
  loading,
  error,
  onClose,
  onReload,
  onReversed
}: {
  open: boolean;
  transaction: TransactionDetail | null;
  loading: boolean;
  error: unknown;
  onClose: () => void;
  onReload: () => void;
  onReversed: (id: string) => void;
}) {
  const [reversalOpen, setReversalOpen] = useState(false);
  const [reason, setReason] = useState<ReversalReasonCode>("operator_correction");
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<unknown>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    setReversalOpen(false);
    setReason("operator_correction");
    setNote("");
    setSubmitError(null);
    setCopied(false);
  }, [open, transaction?.id]);

  const reverse = async () => {
    if (!transaction) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const input = note.trim() ? { reason_code: reason, note: note.trim() } : { reason_code: reason };
      const result = await api.reverseTransaction(transaction.id, input, createIdempotencyKey("console-reversal"));
      setReversalOpen(false);
      onReversed(result.data.transaction_id);
    } catch (caught) {
      setSubmitError(caught);
    } finally {
      setSubmitting(false);
    }
  };

  const copyId = async () => {
    if (!transaction) return;
    await globalThis.navigator.clipboard.writeText(transaction.id);
    setCopied(true);
  };

  return (
    <Dialog open={open} title={transaction ? `${transaction.type[0]?.toUpperCase()}${transaction.type.slice(1)} transaction` : "Transaction inspector"} eyebrow="Journal entry" onClose={onClose} wide>
      {loading ? <Skeleton lines={4} compact /> : error ? <ErrorState description={errorDescription(error, "Transaction detail could not be loaded.")} requestId={isApiError(error) ? error.requestId : undefined} onRetry={onReload} /> : transaction ? (
        <>
          <dl className="detail-meta">
            <div><dt>Amount</dt><dd className="money">{formatMoney(transaction.amount, transaction.currency)}</dd></div>
            <div><dt>State</dt><dd><StatusPill tone={transaction.integrity.balanced ? "positive" : "critical"}>{transaction.integrity.balanced ? "Balanced" : "Needs attention"}</StatusPill></dd></div>
            <div><dt>Transaction ID</dt><dd className="mono">{transaction.id}</dd></div>
            <div><dt>Posted</dt><dd><time dateTime={transaction.created_at}>{formatDubaiDateTime(transaction.created_at)}</time><br /><span className="subtle">{formatUtcDateTime(transaction.created_at)}</span></dd></div>
          </dl>
          <Button variant="quiet" onClick={() => void copyId()}>{copied ? "Copied transaction ID" : "Copy transaction ID"}</Button>
          <PostingBreakdown transaction={transaction} />
          {transaction.reverses_transaction_id || transaction.reversed_by_transaction_id ? (
            <div className="lineage">
              <strong>Reversal pair</strong>
              {transaction.reverses_transaction_id ? <p>This compensating transaction reverses <span className="mono">{shortId(transaction.reverses_transaction_id)}</span>.</p> : null}
              {transaction.reversal_reason_code ? <p>Reason: <strong>{transaction.reversal_reason_code.replaceAll("_", " ")}</strong>{transaction.reversal_note ? ` — ${transaction.reversal_note}` : ""}</p> : null}
              {transaction.reversed_by_transaction_id ? <p>The original entries remain untouched. Reversed by <span className="mono">{shortId(transaction.reversed_by_transaction_id)}</span>.</p> : null}
            </div>
          ) : null}

          {transaction.type !== "reversal" && !transaction.reversed_by_transaction_id ? (
            reversalOpen ? (
              <section className="journal-breakdown" aria-labelledby="reversal-title">
                <h3 id="reversal-title">Reverse this transaction?</h3>
                <p className="subtle">The original journal entries stay untouched. LedgerLite will post an equal and opposite transaction.</p>
                <div className="field">
                  <label htmlFor="reversal-reason">Reason</label>
                  <select id="reversal-reason" value={reason} onChange={(event) => setReason(event.target.value as ReversalReasonCode)}>
                    <option value="duplicate">Duplicate transaction</option>
                    <option value="customer_request">Customer request</option>
                    <option value="operator_correction">Operator correction</option>
                    <option value="other">Other</option>
                  </select>
                </div>
                <div className="field field--spaced">
                  <label htmlFor="reversal-note">Operator note <span className="subtle">(optional)</span></label>
                  <textarea id="reversal-note" maxLength={240} value={note} onChange={(event) => setNote(event.target.value)} placeholder="Why is this compensating entry needed?" />
                </div>
                {submitError ? <div className="scenario-error" role="alert">{isApiError(submitError) && submitError.status === 409 ? "Reversal could not be posted. The transaction may already be reversed, or the returning account has insufficient funds." : errorDescription(submitError, "Reversal could not be posted.")}</div> : null}
                <div className="dialog__actions"><Button onClick={() => setReversalOpen(false)}>Keep transaction</Button><Button variant="danger" busy={submitting} onClick={() => void reverse()}>Post reversal</Button></div>
              </section>
            ) : (
              <div className="dialog__actions"><Button variant="danger" onClick={() => setReversalOpen(true)}>Reverse transaction</Button></div>
            )
          ) : null}
        </>
      ) : null}
    </Dialog>
  );
}

function StatementDialog({
  account,
  page,
  loading,
  error,
  onClose,
  onLoadMore,
  loadingMore,
  loadingMoreError
}: {
  account: Account | null;
  page: StatementPage | null;
  loading: boolean;
  error: unknown;
  onClose: () => void;
  onLoadMore: () => void;
  loadingMore: boolean;
  loadingMoreError: unknown;
}) {
  return (
    <Dialog open={account !== null} title={account?.display_name ?? "Account statement"} eyebrow="Calculated from immutable entries" onClose={onClose} wide>
      {loading ? <Skeleton lines={4} compact /> : error ? <ErrorState description={errorDescription(error, "Statement could not be loaded.")} /> : page ? (
        <>
          <dl className="detail-meta"><div><dt>Calculated balance</dt><dd className="money">{formatMoney(page.balance, page.account.currency)}</dd></div><div><dt>Account</dt><dd className="mono">{page.account.id}</dd></div></dl>
          {page.items.length === 0 ? <EmptyState title="No statement entries" description="This account has no committed journal activity." /> : (
            <div className="table-scroller" tabIndex={0} aria-label="Account statement entries">
              <table className="data-table"><caption className="sr-only">Account statement entries</caption><thead><tr><th scope="col">Posted</th><th scope="col">Type</th><th scope="col">Transaction</th><th scope="col">Entry</th><th scope="col">Balance after</th></tr></thead><tbody>
                {page.items.map((entry) => <tr key={entry.id}><td>{formatDubaiDateTime(entry.created_at)}</td><td><span className={`kind-badge kind-badge--${entry.type}`}>{entry.type}</span></td><td className="mono">{shortId(entry.transaction_id)}</td><td className={`money ${parseMoney(entry.amount) < 0n ? "money--negative" : "money--positive"}`}>{formatMoney(entry.amount, entry.currency, { showPlus: true })}</td><td className="money">{formatMoney(entry.balance_after, entry.currency)}</td></tr>)}
              </tbody></table>
            </div>
          )}
          {page.next_cursor ? loadingMoreError ? <PaginationError description={errorDescription(loadingMoreError, "Older statement entries could not be loaded.")} onRetry={onLoadMore} /> : <div className="load-more"><Button busy={loadingMore} onClick={onLoadMore}>Load older entries</Button></div> : null}
        </>
      ) : null}
    </Dialog>
  );
}

export function LedgerPage({ refreshSignal }: { refreshSignal: number }) {
  const [tab, setTab] = useState<LedgerTab>("transactions");
  const [currency, setCurrency] = useState("AED");
  const [type, setType] = useState<"all" | TransactionType>("all");
  const [accountFilter, setAccountFilter] = useState("");
  const [period, setPeriod] = useState<PeriodFilter>("all");
  const [transactions, setTransactions] = useState<TransactionsPage | null>(null);
  const [accounts, setAccounts] = useState<AccountsPage | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [selectedTransactionId, setSelectedTransactionId] = useState<string | null>(null);
  const [selectedTransaction, setSelectedTransaction] = useState<TransactionDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<unknown>(null);
  const [selectedAccount, setSelectedAccount] = useState<Account | null>(null);
  const [statement, setStatement] = useState<StatementPage | null>(null);
  const [statementLoading, setStatementLoading] = useState(false);
  const [statementError, setStatementError] = useState<unknown>(null);
  const [statementLoadingMore, setStatementLoadingMore] = useState(false);
  const [transactionLoadMoreError, setTransactionLoadMoreError] = useState<unknown>(null);
  const [accountLoadMoreError, setAccountLoadMoreError] = useState<unknown>(null);
  const [statementLoadMoreError, setStatementLoadMoreError] = useState<unknown>(null);
  const normalizedAccountFilter = accountFilter.trim();
  const accountFilterValid = normalizedAccountFilter === "" || UUID_PATTERN.test(normalizedAccountFilter);
  const appliedAccountFilter = normalizedAccountFilter && accountFilterValid ? normalizedAccountFilter : undefined;

  const transactionParams = useMemo(() => ({
    currency: currency || undefined,
    type: type === "all" ? undefined : type,
    account_id: appliedAccountFilter,
    date_from: periodStart(period),
    limit: 25
  }), [appliedAccountFilter, currency, period, type]);

  const transactionData = useAsyncData((signal) => api.listTransactions(transactionParams, signal), [transactionParams, refreshSignal]);
  const accountData = useAsyncData((signal) => api.listAccounts({ currency: currency || undefined, limit: 25 }, signal), [currency, refreshSignal]);

  useEffect(() => { if (transactionData.data) { setTransactions(transactionData.data); setTransactionLoadMoreError(null); } }, [transactionData.data]);
  useEffect(() => { if (accountData.data) { setAccounts(accountData.data); setAccountLoadMoreError(null); } }, [accountData.data]);

  const loadTransaction = useCallback(async (id: string) => {
    setSelectedTransactionId(id);
    setSelectedTransaction(null);
    setDetailError(null);
    setDetailLoading(true);
    try { setSelectedTransaction(await api.transaction(id)); } catch (error) { setDetailError(error); } finally { setDetailLoading(false); }
  }, []);

  const openStatement = useCallback(async (account: Account) => {
    setSelectedAccount(account);
    setStatement(null);
    setStatementError(null);
    setStatementLoadMoreError(null);
    setStatementLoading(true);
    try { setStatement(await api.statement(account.id, { limit: 25 })); } catch (error) { setStatementError(error); } finally { setStatementLoading(false); }
  }, []);

  const loadMoreTransactions = async () => {
    if (!transactions?.next_cursor) return;
    setLoadingMore(true);
    setTransactionLoadMoreError(null);
    try {
      const next = await api.listTransactions({ ...transactionParams, cursor: transactions.next_cursor });
      setTransactions({ items: [...transactions.items, ...next.items], next_cursor: next.next_cursor });
    } catch (error) {
      setTransactionLoadMoreError(error);
    } finally { setLoadingMore(false); }
  };

  const loadMoreAccounts = async () => {
    if (!accounts?.next_cursor) return;
    setLoadingMore(true);
    setAccountLoadMoreError(null);
    try {
      const next = await api.listAccounts({ currency: currency || undefined, limit: 25, cursor: accounts.next_cursor });
      setAccounts({ items: [...accounts.items, ...next.items], next_cursor: next.next_cursor });
    } catch (error) {
      setAccountLoadMoreError(error);
    } finally { setLoadingMore(false); }
  };

  const loadMoreStatement = async () => {
    if (!selectedAccount || !statement?.next_cursor) return;
    setStatementLoadingMore(true);
    setStatementLoadMoreError(null);
    try {
      const next = await api.statement(selectedAccount.id, { limit: 25, cursor: statement.next_cursor });
      setStatement({ ...next, items: [...statement.items, ...next.items] });
    } catch (error) {
      setStatementLoadMoreError(error);
    } finally { setStatementLoadingMore(false); }
  };

  const clearFilters = () => { setCurrency("AED"); setType("all"); setAccountFilter(""); setPeriod("all"); };
  const hasFilters = currency !== "AED" || type !== "all" || accountFilter !== "" || period !== "all";
  const selectTabFromKeyboard = (event: KeyboardEvent<HTMLButtonElement>) => {
    const nextTab = event.key === "ArrowRight" || event.key === "ArrowLeft"
      ? (tab === "transactions" ? "accounts" : "transactions")
      : event.key === "Home"
        ? "transactions"
        : event.key === "End"
          ? "accounts"
          : null;
    if (nextTab === null) return;
    event.preventDefault();
    setTab(nextTab);
    globalThis.requestAnimationFrame(() => {
      document.getElementById(`${nextTab}-tab`)?.focus();
    });
  };

  return (
    <section aria-labelledby="ledger-title">
      <SectionHeading id="ledger-title" eyebrow="Journal / AED default" title="Transaction register" description="Query committed transactions and customer accounts. Open a row for postings, statements, or reversal controls." />
      <div className="view-tabs" role="tablist" aria-label="Ledger view">
        <button id="transactions-tab" role="tab" aria-selected={tab === "transactions"} aria-controls="transactions-panel" tabIndex={tab === "transactions" ? 0 : -1} onKeyDown={selectTabFromKeyboard} onClick={() => setTab("transactions")}>Transactions</button>
        <button id="accounts-tab" role="tab" aria-selected={tab === "accounts"} aria-controls="accounts-panel" tabIndex={tab === "accounts" ? 0 : -1} onKeyDown={selectTabFromKeyboard} onClick={() => setTab("accounts")}>Accounts</button>
      </div>

      <div className="filter-bar" aria-label="Ledger filters">
        {tab === "transactions" ? <div className="field"><label htmlFor="account-filter">Exact account ID</label><div className="input-with-icon"><input id="account-filter" value={accountFilter} onChange={(event) => setAccountFilter(event.target.value)} placeholder="Paste an account UUID" aria-invalid={!accountFilterValid || undefined} aria-describedby={!accountFilterValid ? "account-filter-hint" : undefined} /></div>{!accountFilterValid ? <p className="field__hint field__hint--error" id="account-filter-hint">Enter a complete UUID to apply this filter.</p> : null}</div> : <div className="field"><span className="field__label">Account scope</span><div className="input-with-icon"><input value="Customer accounts only" readOnly aria-label="Account scope" /></div></div>}
        <div className="field"><label htmlFor="currency-filter">Currency</label><select id="currency-filter" value={currency} onChange={(event) => setCurrency(event.target.value)}><option value="AED">AED</option><option value="USD">USD</option><option value="EUR">EUR</option><option value="">All currencies</option></select></div>
        {tab === "transactions" ? <><div className="field"><label htmlFor="type-filter">Type</label><select id="type-filter" value={type} onChange={(event) => setType(event.target.value as "all" | TransactionType)}><option value="all">All types</option><option value="deposit">Deposit</option><option value="transfer">Transfer</option><option value="reversal">Reversal</option></select></div><div className="field"><label htmlFor="period-filter">Period</label><select id="period-filter" value={period} onChange={(event) => setPeriod(event.target.value as PeriodFilter)}><option value="all">All time</option><option value="7d">Last 7 days</option><option value="30d">Last 30 days</option></select></div></> : <div />}
        {hasFilters ? <Button variant="quiet" onClick={clearFilters}>Clear filters</Button> : <span />}
      </div>

      {tab === "transactions" ? (
        <Card className="panel" id="transactions-panel" role="tabpanel" aria-labelledby="transactions-tab" aria-busy={transactionData.loading || undefined}>
          <div className="panel__header"><div><h2>Transaction journal</h2><p>{transactionData.loading ? "Loading active filters…" : `${transactions?.items.length ?? 0} transactions / newest first`}</p></div><StatusPill tone="info">NEWEST FIRST</StatusPill></div>
          {transactionData.error ? <ErrorState description={errorDescription(transactionData.error, "Transactions could not be loaded.")} requestId={isApiError(transactionData.error) ? transactionData.error.requestId : undefined} onRetry={transactionData.refresh} /> : transactionData.loading ? <Skeleton lines={6} compact /> : <TransactionTable items={transactions?.items ?? []} onInspect={(id) => void loadTransaction(id)} />}
          {!transactionData.loading && transactions?.next_cursor ? transactionLoadMoreError ? <PaginationError description={errorDescription(transactionLoadMoreError, "Older transactions could not be loaded.")} onRetry={() => void loadMoreTransactions()} /> : <div className="load-more"><Button busy={loadingMore} onClick={() => void loadMoreTransactions()}>Load older transactions</Button></div> : null}
        </Card>
      ) : (
        <Card className="panel" id="accounts-panel" role="tabpanel" aria-labelledby="accounts-tab" aria-busy={accountData.loading || undefined}>
          <div className="panel__header"><div><h2>Customer accounts</h2><p>Balances calculated from signed entries</p></div><StatusPill tone="positive">CURRENT BALANCES</StatusPill></div>
          {accountData.error ? <ErrorState description={errorDescription(accountData.error, "Accounts could not be loaded.")} requestId={isApiError(accountData.error) ? accountData.error.requestId : undefined} onRetry={accountData.refresh} /> : accountData.loading ? <Skeleton lines={6} compact /> : <AccountsTable items={accounts?.items ?? []} onStatement={(account) => void openStatement(account)} />}
          {!accountData.loading && accounts?.next_cursor ? accountLoadMoreError ? <PaginationError description={errorDescription(accountLoadMoreError, "More accounts could not be loaded.")} onRetry={() => void loadMoreAccounts()} /> : <div className="load-more"><Button busy={loadingMore} onClick={() => void loadMoreAccounts()}>Load more accounts</Button></div> : null}
        </Card>
      )}

      <TransactionInspector open={selectedTransactionId !== null} transaction={selectedTransaction} loading={detailLoading} error={detailError} onClose={() => setSelectedTransactionId(null)} onReload={() => { if (selectedTransactionId) void loadTransaction(selectedTransactionId); }} onReversed={(id) => { setSelectedTransactionId(null); transactionData.refresh(); void loadTransaction(id); }} />
      <StatementDialog account={selectedAccount} page={statement} loading={statementLoading} error={statementError} onClose={() => setSelectedAccount(null)} onLoadMore={() => void loadMoreStatement()} loadingMore={statementLoadingMore} loadingMoreError={statementLoadMoreError} />
    </section>
  );
}
