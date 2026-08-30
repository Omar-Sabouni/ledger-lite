import { useCallback, useEffect, useRef, useState } from "react";

export interface AsyncData<T> {
  data: T | null;
  loading: boolean;
  error: unknown;
  refresh: () => void;
  setData: React.Dispatch<React.SetStateAction<T | null>>;
}

export function useAsyncData<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  dependencies: readonly unknown[] = []
): AsyncData<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [revision, setRevision] = useState(0);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  const refresh = useCallback(() => setRevision((current) => current + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);

    void loaderRef.current(controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) setData(result);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(reason);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });

    return () => controller.abort();
    // Dependencies deliberately control when the latest loader runs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...dependencies, revision]);

  return { data, loading, error, refresh, setData };
}
