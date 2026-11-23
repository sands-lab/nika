import { useEffect, useRef, useState } from "react";

export interface AsyncState<T> {
  data: T | undefined;
  error: Error | undefined;
  loading: boolean;
}

/** Runs an async loader whenever deps change; ignores stale results. Pass null to skip. */
export function useAsync<T>(loader: (() => Promise<T>) | null, deps: unknown[]): AsyncState<T> {
  const [state, setState] = useState<AsyncState<T>>({ data: undefined, error: undefined, loading: !!loader });
  const seq = useRef(0);
  useEffect(() => {
    const id = ++seq.current;
    if (!loader) {
      setState({ data: undefined, error: undefined, loading: false });
      return;
    }
    setState((s) => ({ data: s.data, error: undefined, loading: true }));
    loader().then(
      (data) => {
        if (id === seq.current) setState({ data, error: undefined, loading: false });
      },
      (error: Error) => {
        if (id === seq.current) setState({ data: undefined, error, loading: false });
      },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return state;
}

export function useDebounced<T>(value: T, ms = 250): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const h = setTimeout(() => setV(value), ms);
    return () => clearTimeout(h);
  }, [value, ms]);
  return v;
}

export function useLocalStorage<T>(key: string, initial: T): [T, (v: T) => void] {
  const [v, setV] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? initial : (JSON.parse(raw) as T);
    } catch {
      return initial;
    }
  });
  const set = (nv: T) => {
    setV(nv);
    try {
      localStorage.setItem(key, JSON.stringify(nv));
    } catch {
      /* ignore */
    }
  };
  return [v, set];
}
