import { useEffect, useState } from "react";
import type { BudgetApi, ReceiptDevice } from "./api";
import { qrMatrix } from "./qr";

type DeviceApi = Pick<BudgetApi, "receiptDevices" | "pairReceiptDevice" | "revokeReceiptDevice">;

const copyFor = (locale: string) => locale.startsWith("pl")
  ? {
      title: "Urządzenia", add: "Dodaj urządzenie", revoke: "Odwołaj", empty: "Brak urządzeń.",
      code: "Kod parowania", unavailable: "Kod QR jest niedostępny. Wpisz kod w aplikacji.",
      expires: "Ważny do", error: "Nie udało się wczytać urządzeń.", close: "Zamknij",
    }
  : {
      title: "Devices", add: "Add device", revoke: "Revoke", empty: "No devices yet.",
      code: "Pairing code", unavailable: "A QR code is unavailable. Enter the code in the app.",
      expires: "Expires", error: "Devices could not be loaded.", close: "Close",
    };

function PairingCode({ payload, unavailable }: { payload: string; unavailable: string }) {
  const matrix = qrMatrix(payload);
  if (matrix === null) return <p className="budget-muted">{unavailable}</p>;
  const size = matrix.length;
  const rects = matrix.flatMap((row, y) => row.flatMap((on, x) => (
    on ? <rect key={`${String(x)}-${String(y)}`} x={x} y={y} width={1} height={1} /> : []
  )));
  return (
    <svg className="budget-qr" role="img" aria-label={payload} viewBox={`-4 -4 ${String(size + 8)} ${String(size + 8)}`}
      shapeRendering="crispEdges">
      <rect x={-4} y={-4} width={size + 8} height={size + 8} fill="#fff" />
      <g fill="#111">{rects}</g>
    </svg>
  );
}

export function ReceiptDevices({ api, locale, revision }: { api: DeviceApi; locale: string; revision: number }) {
  const text = copyFor(locale);
  const [hidden, setHidden] = useState(true);
  const [tokens, setTokens] = useState<ReceiptDevice[]>([]);
  const [error, setError] = useState("");
  const [pairing, setPairing] = useState<{ code: string; qr_payload: string; expires_at: number } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void api.receiptDevices().then((result) => {
      if (cancelled) return;
      if (!result.ok) {
        setHidden(result.status === 404);
        setTokens([]);
        setError(result.status === 404 ? "" : text.error);
        return;
      }
      setHidden(false);
      setError("");
      setTokens(Array.isArray(result.data) ? result.data : []);
    });
    return () => { cancelled = true; };
  }, [api, revision, text.error]);

  if (hidden) return null;
  return (
    <section className="budget-devices" aria-label={text.title}>
      <header>
        <h3>{text.title}</h3>
        <button type="button" className="budget-primary" disabled={busy} onClick={() => {
          setBusy(true);
          void api.pairReceiptDevice().then((result) => {
            setBusy(false);
            if (!result.ok) {
              setError(text.error);
              return;
            }
            setError("");
            setPairing(result.data);
          });
        }}>{text.add}</button>
      </header>
      {error === "" ? null : <p className="budget-notice" role="alert">{error}</p>}
      {pairing === null ? null : (
        <div className="budget-pair">
          <p className="budget-muted">{text.code}</p>
          <p className="budget-pair-code">{pairing.code}</p>
          <PairingCode payload={pairing.qr_payload} unavailable={text.unavailable} />
          <p className="budget-muted">{text.expires}: {new Date(pairing.expires_at * 1000).toISOString()}</p>
          <button type="button" onClick={() => { setPairing(null); }}>{text.close}</button>
        </div>
      )}
      {tokens.length === 0 ? <p className="budget-muted">{text.empty}</p> : (
        <ul className="budget-facts">
          {tokens.map((token) => (
            <li key={token.token_id}>
              {token.device_name}
              <button type="button" disabled={busy} onClick={() => {
                setBusy(true);
                void api.revokeReceiptDevice(token.token_id).then((result) => {
                  setBusy(false);
                  if (!result.ok) {
                    setError(text.error);
                    return;
                  }
                  setTokens((current) => current.filter((item) => item.token_id !== token.token_id));
                });
              }}>{text.revoke}</button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
