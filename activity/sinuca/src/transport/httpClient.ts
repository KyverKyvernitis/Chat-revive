export class DashboardHttpError extends Error {
  status: number;
  code: string;
  payload: unknown;

  constructor(message: string, status = 0, code = "request_failed", payload: unknown = null) {
    super(message);
    this.name = "DashboardHttpError";
    this.status = status;
    this.code = code;
    this.payload = payload;
  }
}

function payloadMessage(payload: unknown): string | null {
  if (!payload || typeof payload !== "object") return null;
  const record = payload as Record<string, unknown>;
  for (const key of ["detail", "message", "error", "reason"]) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
}

function normalizedDashboardPath(path: string): string {
  return path.startsWith("/api/") || path === "/api" ? path : `/api${path.startsWith("/") ? path : `/${path}`}`;
}

export async function fetchDashboardJson<T>(path: string, init: RequestInit = {}, timeoutMs = 12000): Promise<T> {
  const normalizedPath = normalizedDashboardPath(path);
  const controller = new AbortController();
  const externalSignal = init.signal;
  let timedOut = false;
  let externallyAborted = Boolean(externalSignal?.aborted);
  const abortFromExternal = () => {
    externallyAborted = true;
    controller.abort(externalSignal?.reason);
  };
  if (externalSignal?.aborted) abortFromExternal();
  else externalSignal?.addEventListener("abort", abortFromExternal, { once: true });
  const timeout = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  try {
    const response = await fetch(normalizedPath, {
      ...init,
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        ...(init.headers || {}),
      },
      signal: controller.signal,
    });
    const contentType = response.headers.get("content-type") || "";
    const raw = await response.text();
    let payload: unknown = null;
    if (raw) {
      if (!contentType.includes("application/json")) {
        throw new DashboardHttpError(
          response.ok ? "A API respondeu em um formato inesperado." : `Falha HTTP ${response.status}.`,
          response.status,
          "invalid_response_type",
          raw.slice(0, 300),
        );
      }
      try {
        payload = JSON.parse(raw);
      } catch {
        throw new DashboardHttpError("A API devolveu JSON inválido.", response.status, "invalid_json", raw.slice(0, 300));
      }
    }
    if (!response.ok) {
      throw new DashboardHttpError(payloadMessage(payload) || `Falha HTTP ${response.status}.`, response.status, "http_error", payload);
    }
    return payload as T;
  } catch (error) {
    if (error instanceof DashboardHttpError) throw error;
    if (controller.signal.aborted || error instanceof DOMException && error.name === "AbortError") {
      if (externallyAborted && !timedOut) throw new DashboardHttpError("Solicitação cancelada.", 0, "aborted");
      throw new DashboardHttpError("A solicitação demorou além do esperado.", 0, "timeout");
    }
    throw new DashboardHttpError(error instanceof Error ? error.message : "Falha de conexão.", 0, "network_error");
  } finally {
    window.clearTimeout(timeout);
    externalSignal?.removeEventListener("abort", abortFromExternal);
  }
}

export async function fetchDashboardNdjson<T>(
  path: string,
  onEvent: (event: T) => void,
  init: RequestInit = {},
  timeoutMs = 12000,
): Promise<void> {
  const controller = new AbortController();
  const externalSignal = init.signal;
  let timedOut = false;
  let externallyAborted = Boolean(externalSignal?.aborted);
  const abortFromExternal = () => {
    externallyAborted = true;
    controller.abort(externalSignal?.reason);
  };
  if (externalSignal?.aborted) abortFromExternal();
  else externalSignal?.addEventListener("abort", abortFromExternal, { once: true });
  const timeout = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  try {
    const response = await fetch(normalizedDashboardPath(path), {
      ...init,
      credentials: "same-origin",
      headers: {
        Accept: "application/x-ndjson",
        ...(init.headers || {}),
      },
      signal: controller.signal,
    });
    const contentType = response.headers.get("content-type") || "";
    if (!response.ok) {
      const raw = await response.text();
      let payload: unknown = raw.slice(0, 300);
      if (raw && contentType.includes("application/json")) {
        try { payload = JSON.parse(raw); } catch { /* A mensagem HTTP continua útil. */ }
      }
      throw new DashboardHttpError(payloadMessage(payload) || `Falha HTTP ${response.status}.`, response.status, "http_error", payload);
    }
    if (!contentType.includes("application/x-ndjson")) {
      const raw = await response.text();
      throw new DashboardHttpError(
        "A API progressiva respondeu em um formato inesperado.",
        response.status,
        "invalid_response_type",
        raw.slice(0, 300),
      );
    }
    if (!response.body) {
      throw new DashboardHttpError("Este navegador não disponibilizou o fluxo de progresso.", response.status, "stream_unavailable");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let receivedEvents = 0;
    const emit = (line: string) => {
      const trimmed = line.trim();
      if (!trimmed) return;
      let event: T;
      try {
        event = JSON.parse(trimmed) as T;
      } catch {
        throw new DashboardHttpError("A API progressiva devolveu um evento inválido.", response.status, "invalid_ndjson", trimmed.slice(0, 300));
      }
      receivedEvents += 1;
      onEvent(event);
    };

    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let newline = buffer.indexOf("\n");
      while (newline >= 0) {
        emit(buffer.slice(0, newline));
        buffer = buffer.slice(newline + 1);
        newline = buffer.indexOf("\n");
      }
      if (done) break;
    }
    emit(buffer);
    if (receivedEvents === 0) {
      throw new DashboardHttpError("A API progressiva terminou sem dados.", response.status, "empty_stream");
    }
  } catch (error) {
    if (error instanceof DashboardHttpError) throw error;
    if (controller.signal.aborted || error instanceof DOMException && error.name === "AbortError") {
      if (externallyAborted && !timedOut) throw new DashboardHttpError("Solicitação cancelada.", 0, "aborted");
      throw new DashboardHttpError("A solicitação demorou além do esperado.", 0, "timeout");
    }
    throw new DashboardHttpError(error instanceof Error ? error.message : "Falha de conexão.", 0, "network_error");
  } finally {
    window.clearTimeout(timeout);
    externalSignal?.removeEventListener("abort", abortFromExternal);
  }
}
