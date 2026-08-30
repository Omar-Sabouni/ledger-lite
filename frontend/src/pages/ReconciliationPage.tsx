import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Dialog } from "../components/Dialog";
import { Button, Card, EmptyState, ErrorState, PaginationError, SectionHeading, Skeleton, StatusPill } from "../components/Ui";
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
  if (item.result === "duplicate") return "Claimed twice";
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
    <Dialog open={open} title={RESULT_LABELS[item.result]} onClose={onClose} wide>
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
  if (items.length === 0) return <EmptyState title="Everything matches" description="" />;
  return (
    <div className="table-scroller" tabIndex={0} aria-label="Reconciliation findings">
      <table className="data-table findings-table"><caption className="sr-only">Bank lines compared with the books</caption><thead><tr><th scope="col">Result</th><th scope="col">Bank reference</th><th scope="col" className="money">Amount</th><th scope="col"><span className="sr-only">Action</span></th></tr></thead><tbody>
        {items.map((item) => <tr key={item.id}><td>{RESULT_LABELS[item.result]}{item.resolution_status !== "open" ? <span className="entry-note"> ({item.resolution_status})</span> : null}</td><td>{item.provider_reference ?? "—"}</td><td className="money">{formatMoney(item.amount, item.currency)}</td><td>{item.resolution_status === "open" && item.result !== "matched" ? <Button variant="quiet" onClick={() => onReview(item)}>Review</Button> : null}</td></tr>)}
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


  return (
    <section aria-labelledby="reconciliation-title">
      <SectionHeading id="reconciliation-title" title="Reconciliation" />
      {runs.error ? <ErrorState title="Reconciliation unavailable" description={isApiError(runs.error) ? runs.error.problem.detail : "Runs could not be loaded."} requestId={isApiError(runs.error) ? runs.error.requestId : undefined} onRetry={runs.refresh} /> : runs.loading && !runs.data ? <Card><Skeleton lines={3} /></Card> : !run ? <Card><EmptyState title="No settlement available" description="Post some entries first." /></Card> : (
        <>
          <p className="balance-statement">
            {run.provider}, {formatDubaiDate(run.period_start)} to {formatDubaiDate(run.period_end)}.{" "}
            {run.status !== "completed" || !counts
              ? "Not checked yet."
              : `${counts.matched.toLocaleString("en-AE")} matched, ${counts.open_exceptions.toLocaleString("en-AE")} still open.`}
            {grossVolume && !grossVolume.difference.replace(/[-0.]/g, "") ? null : grossVolume ? <> The bank is off by <strong>{formatMoney(grossVolume.difference, grossVolume.currency)}</strong>.</> : null}
          </p>
          <p><Button variant="primary" busy={executing} onClick={() => void execute()}>{run.status === "completed" ? "Check again" : "Check against the bank"}</Button>{run.completed_at ? <span className="subtle"> Last checked {formatDubaiDateTime(run.completed_at)}.</span> : null}</p>
          {executeError ? <div className="scenario-error" role="alert">{isApiError(executeError) ? executeError.problem.detail : "The check didn’t finish."}</div> : null}

          {run.status === "pending" ? null : <Card className="panel">
            <div className="panel__header panel__header--filters"><div><h2>Line by line</h2></div><div className="filter-chips" aria-label="Finding filter">{(["all", "open", "matched"] as Filter[]).map((value) => <button key={value} className="filter-chip" type="button" aria-pressed={filter === value} onClick={() => setFilter(value)}>{value === "open" ? "Still open" : value === "all" ? "All" : "Matched"}</button>)}</div></div>
            {itemsError ? <ErrorState description={isApiError(itemsError) ? itemsError.problem.detail : "Findings could not be loaded."} onRetry={() => void loadItems(run, filter)} /> : itemsLoading && !items ? <Skeleton lines={6} compact /> : <FindingTable items={visibleItems} onReview={setSelected} />}
            {items?.next_cursor ? loadMoreError ? <PaginationError description={isApiError(loadMoreError) ? loadMoreError.problem.detail : "More findings could not be loaded."} onRetry={() => void loadMore()} /> : <div className="load-more"><Button busy={loadingMore} onClick={() => void loadMore()}>Load more</Button></div> : null}
          </Card>}
        </>
      )}
      <ItemDialog item={selected} open={selected !== null} onClose={() => setSelected(null)} onResolved={(resolved) => { setItems((current) => current ? { ...current, items: current.items.map((item) => item.id === resolved.id ? resolved : item) } : current); runs.refresh(); }} />
    </section>
  );
}
