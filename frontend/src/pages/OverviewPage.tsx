import { EmptyState, ErrorState, SectionHeading, Skeleton, shortId } from "../components/Ui";
import { useAsyncData } from "../hooks/useAsyncData";
import { api, isApiError } from "../lib/api";
import { formatMoney, isZeroMoney } from "../lib/money";
import { formatDubaiDateTime, formatRelativeTime } from "../lib/time";
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

function plural(count: number, noun: string) {
  return `${count.toLocaleString("en-AE")} ${noun}${count === 1 ? "" : "s"}`;
}

function moneyClass(value: string | undefined) {
  return value?.trim().startsWith("-") ? "money money--negative" : "money";
}

function primaryCurrency(currencies: OverviewCurrency[]): OverviewCurrency | null {
  return currencies.find((item) => item.currency === "AED") ?? currencies[0] ?? null;
}

function RecentTransactions({ items }: { items: TransactionSummary[] }) {
  if (items.length === 0) {
    return <EmptyState title="Nothing posted yet" description="Entries appear here as soon as they are posted." />;
  }
  return (
    <div className="table-scroller" tabIndex={0} aria-label="Recent ledger transactions">
      <table className="data-table">
        <caption className="sr-only">Six most recent ledger transactions</caption>
        <thead>
          <tr><th scope="col">Posted</th><th scope="col">From</th><th scope="col">To</th><th scope="col" className="money">Amount</th></tr>
        </thead>
        <tbody>
          {items.map((transaction) => (
            <tr key={transaction.id}>
              <td title={formatDubaiDateTime(transaction.created_at)}>{formatRelativeTime(transaction.created_at)}</td>
              <td>{transaction.source_display_name ?? shortId(transaction.source_account_id)}</td>
              <td>
                {transaction.destination_display_name ?? shortId(transaction.destination_account_id)}
                {transaction.type === "reversal" ? <span className="entry-note"> (reversal)</span> : null}
              </td>
              <td className="money">{formatMoney(transaction.amount, transaction.currency)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function OverviewPage({ refreshSignal }: OverviewPageProps) {
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
        title="Today’s books"
      />

      {overview.error ? (
        <ErrorState
          title="Couldn’t load the books"
          headingLevel="h2"
          description="The ledger service didn’t answer."
          requestId={isApiError(overview.error) ? overview.error.requestId : undefined}
          onRetry={handleRefresh}
        />
      ) : overview.loading && !overview.data ? (
        <Skeleton lines={3} />
      ) : overview.data ? (
        <>
          <p className={`balance-statement ${integrityVerified ? "" : "balance-statement--off"}`} role="status">
            {integrityVerified
              ? "The books balance."
              : <>The books are off by <strong>{currency ? formatMoney(currency.net_imbalance, currency.currency) : "an unknown amount"}</strong>.</>}
            {" "}
            <span className="as-of">As of <time dateTime={overview.data.as_of}>{formatDubaiDateTime(overview.data.as_of)}</time>.</span>
          </p>

          <section className="register-section" aria-labelledby="aed-totals-title">
            <h2 id="aed-totals-title" className="sr-only">Totals</h2>
            <div className="table-scroller" tabIndex={0} aria-label="Ledger totals">
              <table className="data-table summary-table">
                <tbody>
                  <tr><th scope="row">Customer funds <span className="entry-note">({currency?.customer_accounts ?? 0} accounts)</span></th><td className={moneyClass(currency?.total_customer_funds)}>{currency ? formatMoney(currency.total_customer_funds, currency.currency) : "—"}</td></tr>
                  <tr><th scope="row">Clearing</th><td className={moneyClass(currency?.clearing_balance)}>{currency ? formatMoney(currency.clearing_balance, currency.currency) : "—"}</td></tr>
                  <tr className="rule-above"><th scope="row">Net</th><td className={moneyClass(currency?.net_imbalance)}>{currency ? formatMoney(currency.net_imbalance, currency.currency) : "—"}</td></tr>
                </tbody>
              </table>
            </div>
            <p className="subtle">{plural(overview.data.integrity.transaction_count, "transaction")}, {plural(overview.data.integrity.reversal_count, "reversal")}, {overview.data.integrity.open_reconciliation_exceptions === 0 ? "nothing unmatched at the bank" : `${plural(overview.data.integrity.open_reconciliation_exceptions, "unmatched bank line")}`}.</p>
          </section>

          <section className="register-section" aria-labelledby="recent-title">
            <h2 id="recent-title">Latest entries</h2>
            {recent.error ? <ErrorState description="Couldn’t load the latest entries." onRetry={recent.refresh} /> : recent.loading && !recent.data ? <Skeleton lines={4} compact /> : <RecentTransactions items={recent.data?.items ?? []} />}
            <p className="section-link"><a href="#ledger">Open the journal</a></p>
          </section>
        </>
      ) : null}
    </section>
  );
}
