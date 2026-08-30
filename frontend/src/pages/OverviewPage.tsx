import { Button, Card, EmptyState, ErrorState, SectionHeading, Skeleton, StatusPill, shortId } from "../components/Ui";
import { useAsyncData } from "../hooks/useAsyncData";
import { api, isApiError } from "../lib/api";
import { formatMoney, isZeroMoney } from "../lib/money";
import { formatDubaiDateTime, formatRelativeTime, formatUtcDateTime } from "../lib/time";
import type { LedgerEvent, OverviewCurrency, TransactionSummary } from "../lib/types";

interface OverviewPageProps {
  refreshSignal: number;
  apiReady: boolean | null;
  eventReady: boolean;
  lastEvent: LedgerEvent | null;
}

function safeZero(value: string) {
  try {
    return isZeroMoney(value);
  } catch {
    return false;
  }
}

function primaryCurrency(currencies: OverviewCurrency[]): OverviewCurrency | null {
  return currencies.find((item) => item.currency === "AED") ?? currencies[0] ?? null;
}

function RecentTransactions({ items }: { items: TransactionSummary[] }) {
  if (items.length === 0) {
    return <EmptyState title="No ledger activity yet" description="Add ledger transactions to populate this console." />;
  }
  return (
    <div className="table-scroller" tabIndex={0} aria-label="Recent ledger transactions">
      <table className="data-table">
        <caption className="sr-only">Six most recent ledger transactions</caption>
        <thead>
          <tr><th scope="col">Posted</th><th scope="col">Type</th><th scope="col">Route</th><th scope="col">Amount</th><th scope="col">State</th></tr>
        </thead>
        <tbody>
          {items.map((transaction) => (
            <tr key={transaction.id}>
              <td title={formatDubaiDateTime(transaction.created_at)}>{formatRelativeTime(transaction.created_at)}</td>
              <td><span className={`kind-badge kind-badge--${transaction.type}`}>{transaction.type}</span></td>
              <td>
                <span className="account-route">
                  <span>{transaction.source_display_name ?? shortId(transaction.source_account_id)}</span>
                  <span className="account-route__arrow" aria-hidden="true">→</span>
                  <span>{transaction.destination_display_name ?? shortId(transaction.destination_account_id)}</span>
                </span>
              </td>
              <td className="money">{formatMoney(transaction.amount, transaction.currency)}</td>
              <td><StatusPill tone="positive">Balanced</StatusPill></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function OverviewPage({ refreshSignal, apiReady, eventReady, lastEvent }: OverviewPageProps) {
  const overview = useAsyncData((signal) => api.overview(signal), [refreshSignal]);
  const recent = useAsyncData((signal) => api.listTransactions({ limit: 6 }, signal), [refreshSignal]);
  const currency = overview.data ? primaryCurrency(overview.data.currencies) : null;
  const integrityVerified = Boolean(
    overview.data &&
    overview.data.integrity.unbalanced_transaction_count === 0 &&
    overview.data.currencies.every((item) => safeZero(item.net_imbalance))
  );

  const handleRefresh = () => {
    overview.refresh();
    recent.refresh();
  };

  return (
    <section aria-labelledby="overview-title">
      <SectionHeading
        id="overview-title"
        eyebrow="Ledger / AED"
        title="Daily ledger position"
        description="Current balances, journal activity, reconciliation workload, and service state."
        action={<Button onClick={handleRefresh} busy={overview.loading}>Refresh snapshot</Button>}
      />

      {overview.error ? (
        <ErrorState
          title="Overview unavailable"
          headingLevel="h2"
          description="The service could not return the current ledger snapshot."
          requestId={isApiError(overview.error) ? overview.error.requestId : undefined}
          onRetry={handleRefresh}
        />
      ) : overview.loading && !overview.data ? (
        <Card><Skeleton lines={3} /></Card>
      ) : overview.data ? (
        <>
          <div className={`integrity-register ${integrityVerified ? "" : "integrity-register--warning"}`} role="status">
            <span className="integrity-register__code">JOURNAL</span>
            <div className="integrity-register__statement">
              <strong>{integrityVerified ? "BALANCED" : "REVIEW REQUIRED"}</strong>
              <span>{integrityVerified ? "Every transaction has two currency-consistent postings and the journal sums to zero." : "The snapshot contains an imbalance. Review the journal before using these totals."}</span>
            </div>
            <div className="integrity-register__balance">
              <span>Net imbalance</span>
              <strong>{currency ? formatMoney(currency.net_imbalance, currency.currency) : "No currency"}</strong>
            </div>
          </div>

          <div className="as-of">
            <span>SNAPSHOT</span>
            <time dateTime={overview.data.as_of}>{formatDubaiDateTime(overview.data.as_of)}</time>
            <span className="as-of__separator">/</span>
            <span>{formatUtcDateTime(overview.data.as_of)}</span>
          </div>

          <section className="register-section" aria-labelledby="aed-totals-title">
            <div className="panel__header">
              <div><h2 id="aed-totals-title">AED totals</h2><p>Book position and journal counts at snapshot time</p></div>
              <StatusPill tone={integrityVerified ? "positive" : "warning"}>{integrityVerified ? "Balanced" : "Review required"}</StatusPill>
            </div>
            <div className="table-scroller" tabIndex={0} aria-label="AED ledger control totals">
              <table className="data-table summary-table">
                <thead><tr><th scope="col">Control account</th><th scope="col">Value</th><th scope="col">Basis</th></tr></thead>
                <tbody>
                  <tr><th scope="row">Customer funds</th><td className="money">{currency ? formatMoney(currency.total_customer_funds, currency.currency) : "—"}</td><td>{currency ? `${currency.customer_accounts} customer accounts` : "No currency data"}</td></tr>
                  <tr><th scope="row">Clearing position</th><td className="money">{currency ? formatMoney(currency.clearing_balance, currency.currency) : "—"}</td><td>System counter-position</td></tr>
                  <tr><th scope="row">Transactions</th><td className="money">{overview.data.integrity.transaction_count.toLocaleString("en-AE")}</td><td>{overview.data.integrity.entry_count.toLocaleString("en-AE")} immutable postings</td></tr>
                  <tr><th scope="row">Reversals</th><td className="money">{overview.data.integrity.reversal_count.toLocaleString("en-AE")}</td><td>Compensating transactions</td></tr>
                  <tr><th scope="row">Idempotent replays</th><td className="money">{overview.data.integrity.replay_count.toLocaleString("en-AE")}</td><td>Requests replayed without moving funds twice</td></tr>
                  <tr><th scope="row">Open reconciliation exceptions</th><td className="money">{overview.data.integrity.open_reconciliation_exceptions.toLocaleString("en-AE")}</td><td>Items awaiting operator review</td></tr>
                </tbody>
              </table>
            </div>
          </section>

          <div className="content-grid">
            <Card className="panel">
              <div className="panel__header">
                <div><h2>Recent postings</h2><p>Six newest committed transactions</p></div>
                <span className="live-label">LIVE FEED</span>
              </div>
              {recent.error ? <ErrorState description="Recent transactions could not be loaded." onRetry={recent.refresh} /> : recent.loading && !recent.data ? <Skeleton lines={4} compact /> : <RecentTransactions items={recent.data?.items ?? []} />}
              <div className="panel__footer"><a className="button button--quiet" href="#ledger">Open journal →</a></div>
            </Card>

            <Card className="panel">
              <div className="panel__header"><div><h2>Posting rules</h2><p>How journal entries are recorded</p></div></div>
              <dl className="control-list">
                <div><dt>Posting count</dt><dd><strong>02</strong><span>per transaction</span></dd></div>
                <div><dt>Journal rows</dt><dd><strong>FIXED</strong><span>corrections use reversals</span></dd></div>
                <div><dt>Transfers</dt><dd><strong>ALL OR NONE</strong><span>both postings move together</span></dd></div>
                <div><dt>Balances</dt><dd><strong>CALCULATED</strong><span>sum of signed entries</span></dd></div>
              </dl>
              <div className="panel__header panel__header--subsection"><div><h2>System state</h2><p>Readiness and committed-event stream</p></div></div>
              <ul className="system-list">
                <li><span className="system-list__copy"><strong>Ledger service</strong><span>Requests and journal reads</span></span><StatusPill tone={apiReady ? "positive" : apiReady === false ? "critical" : "neutral"}>{apiReady === null ? "Checking" : apiReady ? "Ready" : "Unavailable"}</StatusPill></li>
                <li><span className="system-list__copy"><strong>Database</strong><span>Account and journal storage</span></span><StatusPill tone={apiReady ? "positive" : apiReady === false ? "critical" : "neutral"}>{apiReady ? "Connected" : apiReady === false ? "Unavailable" : "Checking"}</StatusPill></li>
                <li><span className="system-list__copy"><strong>Event stream</strong><span>{lastEvent ? `Last: ${lastEvent.event_type}` : "Committed events only"}</span></span><StatusPill tone={eventReady ? "positive" : "warning"}>{eventReady ? "Streaming" : "Reconnecting"}</StatusPill></li>
              </ul>
            </Card>
          </div>
        </>
      ) : null}
    </section>
  );
}
