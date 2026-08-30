import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Dialog } from "../components/Dialog";
import { Button, Card, EmptyState, ErrorState, PaginationError, SectionHeading, Skeleton, StatusPill, shortId } from "../components/Ui";
import { useAsyncData } from "../hooks/useAsyncData";
import { api, isApiError } from "../lib/api";
import { formatMoney } from "../lib/money";
import { formatDubaiDate, formatDubaiDateTime } from "../lib/time";
import type { ReconciliationItem, ReconciliationItemListParams, ReconciliationItemResult, ReconciliationItemsPage, ReconciliationRun } from "../lib/types";

type Filter = "all" | "open" | ReconciliationItemResult;

const RESULT_LABELS: Record<ReconciliationItemResult, string> = {
  pending: "Pending comparison",
  matched: "Matched",
  provider_only: "Provider only",
  ledger_only: "Ledger only",
  mismatched: "Field mismatch",
  duplicate: "Duplicate claim"
};

function toneForItem(item: ReconciliationItem): "positive" | "warning" | "neutral" | "info" {
  if (item.resolution_status !== "open") return "info";
  return item.result === "matched" ? "positive" : item.result === "pending" ? "neutral" : "warning";
}

function itemQuery(filter: Filter, cursor?: string): ReconciliationItemListParams {
  if (filter === "open") return { resolution_status: "open", limit: 50, cursor };
  if (filter !== "all") return { result: filter, limit: 50, cursor };
  return { limit: 50, cursor };
}

function ledgerAmountComparison(item: ReconciliationItem): string {
  if (item.result === "provider_only") return "No ledger record";
  if (item.mismatch_code === "amount_mismatch") return "Amount differs";
  if (item.mismatch_code === "currency_mismatch") return "Currency differs";
  if (item.mismatch_code === "transaction_type_mismatch") return "Ineligible transaction type";
  if (item.result === "duplicate") return "Duplicate claim — not asserted";
  return formatMoney(item.amount, item.currency);
}

function ItemDialog({ item, open, onClose, onResolved }: { item: ReconciliationItem | null; open: boolean; onClose: () => void; onResolved: (item: ReconciliationItem) => void }) {
  const [mode, setMode] = useState<"none" | "match" | "ignore">("none");
  const [transactionId, setTransactionId] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => { if (!open) { setMode("none"); setTransactionId(""); setNote(""); setError(null); } }, [open]);

  const resolve = async () => {
    if (!item) return;
    setBusy(true);
    setError(null);
    try {
      const resolved = mode === "match"
        ? await api.matchReconciliationItem(item.id, note.trim() ? { transaction_id: transactionId.trim(), note: note.trim() } : { transaction_id: transactionId.trim() })
        : await api.ignoreReconciliationItem(item.id, { reason: note.trim() });
      onResolved(resolved);
      onClose();
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  };

  if (!item) return null;
  const canResolve = item.resolution_status === "open" && item.result !== "matched" && item.result !== "pending";
  const canMatch = canResolve && item.result !== "ledger_only";

  return (
    <Dialog open={open} title={RESULT_LABELS[item.result]} eyebrow="Reconciliation finding" onClose={onClose} wide>
      <div className="record-compare">
        <section className="record-card"><h3>Processor record</h3><dl><div><dt>Provider reference</dt><dd className="mono">{item.provider_reference ?? "No provider record"}</dd></div><div><dt>Claimed transaction</dt><dd className="mono">{item.claimed_transaction_id ?? "Not supplied"}</dd></div><div><dt>Processor amount</dt><dd className="money">{item.result === "ledger_only" ? "—" : formatMoney(item.amount, item.currency)}</dd></div><div><dt>Occurred</dt><dd>{formatDubaiDateTime(item.occurred_at)}</dd></div></dl></section>
        <span className="compare-mark" aria-hidden="true">↔</span>
        <section className="record-card"><h3>Ledger record</h3><dl><div><dt>Matched transaction</dt><dd className="mono">{item.matched_transaction_id ?? "No confirmed match"}</dd></div><div><dt>Amount comparison</dt><dd className="money">{ledgerAmountComparison(item)}</dd></div><div><dt>Finding code</dt><dd>{item.mismatch_code?.replaceAll("_", " ") ?? "exact_match"}</dd></div><div><dt>Resolution</dt><dd><StatusPill tone={toneForItem(item)}>{item.resolution_status}</StatusPill></dd></div></dl></section>
      </div>

      {item.resolution_status !== "open" ? <div className="lineage"><strong>{item.resolution_status === "matched" ? "Manually matched" : "Ignored with reason"}</strong><p>{item.resolution_note ?? "Resolution recorded without changing the ledger."}</p></div> : null}

      {canResolve && mode === "none" ? <div className="dialog__actions"><Button onClick={() => setMode("ignore")}>Ignore exception</Button>{canMatch ? <Button variant="primary" onClick={() => setMode("match")}>Match manually</Button> : null}</div> : null}
      {canResolve && mode !== "none" ? (
        <section className="journal-breakdown">
          <h3>{mode === "match" ? "Match to a ledger transaction" : "Ignore this exception"}</h3>
          {mode === "match" ? <div className="field"><label htmlFor="match-transaction">Exact transaction UUID</label><input id="match-transaction" value={transactionId} onChange={(event) => setTransactionId(event.target.value)} placeholder="00000000-0000-0000-0000-000000000000" required /></div> : null}
          <div className="field field--spaced"><label htmlFor="resolution-note">{mode === "ignore" ? "Required reason" : "Operator note (optional)"}</label><textarea id="resolution-note" value={note} onChange={(event) => setNote(event.target.value)} maxLength={240} placeholder={mode === "ignore" ? "Why should this exception be excluded?" : "Note about this match"} required={mode === "ignore"} /></div>
          {error ? <div className="scenario-error" role="alert">{isApiError(error) ? error.problem.detail : "The resolution could not be recorded."}</div> : null}
          <div className="dialog__actions"><Button onClick={() => setMode("none")}>Cancel</Button><Button variant="primary" busy={busy} disabled={(mode === "match" && !transactionId.trim()) || (mode === "ignore" && !note.trim())} onClick={() => void resolve()}>{mode === "match" ? "Confirm manual match" : "Ignore with reason"}</Button></div>
        </section>
      ) : null}
    </Dialog>
  );
}

function FindingTable({ items, onReview }: { items: ReconciliationItem[]; onReview: (item: ReconciliationItem) => void }) {
  if (items.length === 0) return <EmptyState code="00" title="No exceptions found" description="Every compared provider record agrees with the immutable ledger for this filter." />;
  return (
    <div className="table-scroller" tabIndex={0} aria-label="Reconciliation findings">
      <table className="data-table findings-table"><caption className="sr-only">Processor-to-ledger reconciliation findings</caption><thead><tr><th scope="col">Finding</th><th scope="col">Provider ref</th><th scope="col">Claimed transaction</th><th scope="col">Amount</th><th scope="col">Status</th><th scope="col"><span className="sr-only">Action</span></th></tr></thead><tbody>
        {items.map((item) => <tr key={item.id}><td><span className={`kind-badge ${item.result === "matched" ? "kind-badge--deposit" : "kind-badge--reversal"}`}>{RESULT_LABELS[item.result]}</span></td><td className="mono">{item.provider_reference ?? "—"}</td><td className="mono">{shortId(item.claimed_transaction_id)}</td><td className="money">{formatMoney(item.amount, item.currency)}</td><td><StatusPill tone={toneForItem(item)}>{item.resolution_status === "open" ? item.result.replaceAll("_", " ") : item.resolution_status}</StatusPill></td><td><Button variant="quiet" onClick={() => onReview(item)}>Review</Button></td></tr>)}
      </tbody></table>
    </div>
  );
}

export function ReconciliationPage({ refreshSignal }: { refreshSignal: number }) {
  const runs = useAsyncData((signal) => api.listReconciliationRuns(signal), [refreshSignal]);
  const [run, setRun] = useState<ReconciliationRun | null>(null);
  const [items, setItems] = useState<ReconciliationItemsPage | null>(null);
  const [itemsLoading, setItemsLoading] = useState(false);
  const [itemsError, setItemsError] = useState<unknown>(null);
  const [executing, setExecuting] = useState(false);
  const [executeError, setExecuteError] = useState<unknown>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [selected, setSelected] = useState<ReconciliationItem | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadMoreError, setLoadMoreError] = useState<unknown>(null);
  const itemsRequest = useRef(0);
  const integrity = useAsyncData((signal) => api.overview(signal), [refreshSignal]);

  const loadItems = useCallback(async (activeRun: ReconciliationRun, activeFilter: Filter, signal?: AbortSignal) => {
    const request = ++itemsRequest.current;
    setItemsLoading(true); setItemsError(null);
    setLoadingMore(false);
    setLoadMoreError(null);
    setItems(null);
    try {
      const page = await api.listReconciliationItems(activeRun.id, itemQuery(activeFilter), signal);
      if (request === itemsRequest.current && !signal?.aborted) setItems(page);
    } catch (error) {
      if (request === itemsRequest.current && !signal?.aborted) setItemsError(error);
    } finally {
      if (request === itemsRequest.current && !signal?.aborted) setItemsLoading(false);
    }
  }, []);

  useEffect(() => {
    const next = runs.data?.items.find((candidate) => candidate.currency === "AED") ?? runs.data?.items[0] ?? null;
    setRun(next);
  }, [runs.data]);

  useEffect(() => {
    if (!run) {
      itemsRequest.current += 1;
      setItems(null);
      setItemsLoading(false);
      return;
    }
    const controller = new AbortController();
    void loadItems(run, filter, controller.signal);
    return () => controller.abort();
  }, [filter, loadItems, run]);

  const execute = async () => {
    if (!run) return;
    setExecuting(true); setExecuteError(null);
    try { const completed = await api.executeReconciliation(run.id); setRun(completed); } catch (error) { setExecuteError(error); } finally { setExecuting(false); }
  };

  const visibleItems = useMemo(() => {
    const all = items?.items ?? [];
    if (filter === "all") return all;
    if (filter === "open") return all.filter((item) => item.resolution_status === "open" && item.result !== "matched");
    return all.filter((item) => item.result === filter);
  }, [filter, items]);

  const counts = run?.summary?.counts;
  const grossVolume = run?.summary?.gross_volume;

  const loadMore = async () => {
    if (!run || !items?.next_cursor) return;
    const request = itemsRequest.current;
    setLoadingMore(true);
    setLoadMoreError(null);
    try {
      const next = await api.listReconciliationItems(run.id, itemQuery(filter, items.next_cursor));
      if (request === itemsRequest.current) setItems({ items: [...items.items, ...next.items], next_cursor: next.next_cursor });
    } catch (error) {
      if (request === itemsRequest.current) setLoadMoreError(error);
    } finally {
      if (request === itemsRequest.current) setLoadingMore(false);
    }
  };

  const unbalancedTransactions = integrity.data?.integrity.unbalanced_transaction_count ?? null;

  return (
    <section aria-labelledby="reconciliation-title">
      <SectionHeading id="reconciliation-title" eyebrow="Processor settlement" title="Reconciliation worksheet" description="Compare processor records with committed ledger transactions. Manual decisions add notes without rewriting the journal." />
      {runs.error ? <ErrorState title="Reconciliation unavailable" description={isApiError(runs.error) ? runs.error.problem.detail : "Runs could not be loaded."} requestId={isApiError(runs.error) ? runs.error.requestId : undefined} onRetry={runs.refresh} /> : runs.loading && !runs.data ? <Card><Skeleton lines={3} /></Card> : !run ? <Card><EmptyState title="No settlement available" description="Add ledger transactions to create an AED processor settlement." /></Card> : (
        <>
          <div className="reconciliation-hero">
            <Card className="run-card">
              <div className="run-card__top"><div><h2>{run.provider} / settlement run</h2><p>Settlement batch</p></div><StatusPill tone={run.status === "completed" ? "positive" : "warning"}>{run.status.toUpperCase()}</StatusPill></div>
              <dl className="run-card__meta"><div><dt>Period</dt><dd>{formatDubaiDate(run.period_start)} – {formatDubaiDate(run.period_end)}</dd></div><div><dt>Currency</dt><dd>{run.currency}</dd></div><div><dt>Last completed</dt><dd>{run.completed_at ? formatDubaiDateTime(run.completed_at) : "Not executed"}</dd></div></dl>
              <div className="run-card__footer"><span className="subtle">Processor records remain unchanged.</span><Button variant="primary" busy={executing} onClick={() => void execute()}>{run.status === "completed" ? "Run again" : "Run reconciliation"}</Button></div>
              {executeError ? <div className="scenario-error" role="alert">{isApiError(executeError) ? executeError.problem.detail : "The reconciliation run could not complete."}</div> : null}
            </Card>
            <Card className={`integrity-card ${unbalancedTransactions === null ? "integrity-card--unknown" : unbalancedTransactions > 0 ? "integrity-card--warning" : ""}`}>
              <div className="integrity-card__top"><div><h2>Journal balance</h2><p>Separate from processor agreement</p></div><span className="integrity-card__code">JOURNAL</span></div>
              <div className="integrity-card__value"><strong>{unbalancedTransactions === null ? "—" : unbalancedTransactions.toLocaleString("en-AE")}</strong><span>{integrity.error ? "balance unavailable" : integrity.loading && !integrity.data ? "checking transactions" : "unbalanced transactions"}</span></div>
            </Card>
          </div>

          <div className="table-scroller recon-summary" tabIndex={0} aria-label="Reconciliation totals">
            <table className="data-table summary-table">
              <thead><tr><th scope="col">Measure</th><th scope="col">Value</th><th scope="col">Notes</th></tr></thead>
              <tbody>
                <tr><th scope="row">Matched records</th><td className="money">{(counts?.matched ?? 0).toLocaleString("en-AE")}</td><td>Automatic or operator-confirmed</td></tr>
                <tr><th scope="row">Open exceptions</th><td className="money">{(counts?.open_exceptions ?? 0).toLocaleString("en-AE")}</td><td>Ledger remains unchanged</td></tr>
                <tr><th scope="row">Gross volume difference</th><td className="money">{grossVolume ? formatMoney(grossVolume.difference, grossVolume.currency) : "—"}</td><td>{grossVolume ? `${formatMoney(grossVolume.provider_total, grossVolume.currency)} processor gross` : "Run comparison to calculate"}</td></tr>
              </tbody>
            </table>
          </div>

          <Card className="panel">
            <div className="panel__header panel__header--filters"><div><h2>Settlement findings</h2><p>Classifications and operator decisions</p></div><div className="filter-chips" aria-label="Finding filter">{(["all", "open", "matched", "provider_only", "ledger_only", "mismatched", "duplicate"] as Filter[]).map((value) => <button key={value} className="filter-chip" type="button" aria-pressed={filter === value} onClick={() => setFilter(value)}>{value === "open" ? "Unresolved" : value.replaceAll("_", " ")}</button>)}</div></div>
            {run.status === "pending" ? <EmptyState title="No reconciliation run yet" description="Run the settlement to compare processor records with the ledger." action={<Button variant="primary" busy={executing} onClick={() => void execute()}>Run reconciliation</Button>} /> : itemsError ? <ErrorState description={isApiError(itemsError) ? itemsError.problem.detail : "Findings could not be loaded."} onRetry={() => void loadItems(run, filter)} /> : itemsLoading && !items ? <Skeleton lines={6} compact /> : <FindingTable items={visibleItems} onReview={setSelected} />}
            {items?.next_cursor ? loadMoreError ? <PaginationError description={isApiError(loadMoreError) ? loadMoreError.problem.detail : "More findings could not be loaded."} onRetry={() => void loadMore()} /> : <div className="load-more"><Button busy={loadingMore} onClick={() => void loadMore()}>Load more findings</Button></div> : null}
          </Card>
        </>
      )}
      <ItemDialog item={selected} open={selected !== null} onClose={() => setSelected(null)} onResolved={(resolved) => { setItems((current) => current ? { ...current, items: current.items.map((item) => item.id === resolved.id ? resolved : item) } : current); runs.refresh(); }} />
    </section>
  );
}
