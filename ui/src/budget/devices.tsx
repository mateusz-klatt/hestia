import { useCallback, useEffect, useRef, useState } from "react";
import { t } from "../i18n";
import type { BudgetApi, BudgetResult, ReceiptDevice, ReceiptPair } from "./api";
import { qrMatrix } from "./qr";

type DeviceApi = Pick<BudgetApi, "receiptDevices" | "pairReceiptDevice" | "cancelReceiptPairCode" | "revokeReceiptDevice">;
type AccessBarrier = (result: { ok: boolean; status?: number; error?: string }) => boolean;

function formatExpiry(expiresAt: number, now: number, locale: string): string {
  const expiry = new Date(expiresAt);
  const today = new Date(now);
  const sameDay = expiry.getFullYear() === today.getFullYear() && expiry.getMonth() === today.getMonth()
    && expiry.getDate() === today.getDate();
  const date = sameDay ? {} : { day: "numeric", month: "short", year: "numeric" } as const;
  return new Intl.DateTimeFormat(locale, { ...date, hour: "2-digit", minute: "2-digit" }).format(expiry);
}

function formatCountdown(expiresAt: number, now: number): string {
  const seconds = Math.max(0, Math.ceil((expiresAt - now) / 1000));
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

function PairingCode({ payload, label, unavailable }: { payload: string; label: string; unavailable: string }) {
  const matrix = qrMatrix(payload);
  if (matrix === null) return <p className="budget-muted">{unavailable}</p>;
  const size = matrix.length;
  const rects = matrix.flatMap((row, y) => row.flatMap((on, x) => (
    on ? <rect key={`${String(x)}-${String(y)}`} x={x} y={y} width={1} height={1} /> : []
  )));
  return (
    <svg className="budget-qr" role="img" aria-label={label} viewBox={`-4 -4 ${String(size + 8)} ${String(size + 8)}`}
      shapeRendering="crispEdges">
      <rect x={-4} y={-4} width={size + 8} height={size + 8} fill="#fff" />
      <g fill="#111">{rects}</g>
    </svg>
  );
}

export function ReceiptDevices({ api, locale, revision, onAccessLoss, focusRevision = 0 }: {
  api: DeviceApi; locale: string; revision: number; onAccessLoss: AccessBarrier; focusRevision?: number;
}) {
  const [hidden, setHidden] = useState(true);
  const [devices, setDevices] = useState<ReceiptDevice[]>([]);
  const [error, setError] = useState("");
  const [pairing, setPairing] = useState<ReceiptPair | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [busy, setBusy] = useState(false);
  const [listRevision, setListRevision] = useState(0);
  const listGeneration = useRef(0);
  const actionGeneration = useRef(0);
  const sectionRef = useRef<HTMLElement>(null);

  const hide = useCallback(() => {
    listGeneration.current += 1;
    actionGeneration.current += 1;
    setHidden(true);
    setDevices([]);
    setPairing(null);
    setBusy(false);
  }, []);

  useEffect(() => {
    const generation = ++listGeneration.current;
    void api.receiptDevices().then((result) => {
      if (onAccessLoss(result) || generation !== listGeneration.current) return;
      if (!result.ok && result.status === 404) {
        hide();
        return;
      }
      setHidden(false);
      setError(result.ok ? "" : t("budget.devices.listError"));
      setDevices(result.ok && Array.isArray(result.data) ? result.data : []);
    });
    return () => { listGeneration.current += 1; };
  }, [api, revision, listRevision, onAccessLoss, hide]);

  useEffect(() => () => { actionGeneration.current += 1; }, [api]);

  useEffect(() => {
    if (hidden || focusRevision === 0) return;
    sectionRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [hidden, focusRevision]);

  useEffect(() => {
    if (pairing === null) return;
    setNow(Date.now());
    const timer = setInterval(() => { setNow(Date.now()); }, 1000);
    return () => { clearInterval(timer); };
  }, [pairing]);

  useEffect(() => {
    if (pairing === null) return;
    const timer = setTimeout(() => { setPairing(null); }, Math.max(0, pairing.expires_at * 1000 - Date.now()));
    return () => { clearTimeout(timer); };
  }, [pairing]);

  const run = async <T,>(action: () => Promise<BudgetResult<T>>, accept: (data: T) => void, message: string) => {
    const generation = ++actionGeneration.current;
    setBusy(true);
    setError("");
    const result = await action();
    if (onAccessLoss(result) || generation !== actionGeneration.current) return;
    setBusy(false);
    if (result.ok) accept(result.data);
    else if (result.status === 404) hide();
    else setError(message);
  };

  const closePairing = () => {
    setPairing(null);
    setListRevision((current) => current + 1);
  };

  if (hidden) return null;
  const title = t("budget.devices.title");
  return (
    <section className="budget-devices" aria-label={title} ref={sectionRef}>
      <header>
        <h3>{title}</h3>
        <button type="button" className="budget-primary" disabled={busy}
          onClick={() => { void run(api.pairReceiptDevice, setPairing, t("budget.devices.pairError")); }}>{t("budget.devices.add")}</button>
      </header>
      {error === "" ? null : <p className="budget-notice" role="alert">{error}</p>}
      {pairing === null ? null : (
        <div className="budget-pair">
          <p className="budget-muted">{t("budget.devices.code")}</p>
          <p className="budget-pair-code">{pairing.code}</p>
          <PairingCode payload={pairing.qr_payload} label={t("budget.devices.qr")} unavailable={t("budget.devices.unavailable")} />
          <p className="budget-muted">
            {t("budget.devices.expires", { time: formatExpiry(pairing.expires_at * 1000, now, locale) })} · {t("budget.devices.remainingLabel")} <span role="timer">{formatCountdown(pairing.expires_at * 1000, now)}</span>
          </p>
          <div className="budget-actions">
            <button type="button" onClick={closePairing}>{t("budget.devices.close")}</button>
            <button type="button" disabled={busy} onClick={() => {
              void run(() => api.cancelReceiptPairCode(pairing.code_id), closePairing, t("budget.devices.cancelError"));
            }}>{t("budget.devices.cancel")}</button>
          </div>
        </div>
      )}
      {devices.length === 0 ? <p className="budget-muted">{t("budget.devices.empty")}</p> : (
        <ul className="budget-facts">
          {devices.map((device) => (
            <li key={device.token_id}>
              {device.device_name}
              <button type="button" disabled={busy} onClick={() => {
                void run(() => api.revokeReceiptDevice(device.token_id), () => {
                  listGeneration.current += 1;
                  setDevices((current) => current.filter((item) => item.token_id !== device.token_id));
                }, t("budget.devices.revokeError"));
              }}>{t("budget.devices.revoke")}</button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
