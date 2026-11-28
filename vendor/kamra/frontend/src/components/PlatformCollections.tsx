import { useCallback, useEffect, useState } from "react"
import { Link } from "react-router-dom"
import { Wallet } from "lucide-react"
import {
  call,
  getCurrentProperty,
  type PlatformCollectionRow,
  type PlatformCollections as Data,
  type PlatformCollectionState,
} from "../lib/api"
import { Card, CardContent, CardHeader, CardTitle } from "./ui/card"
import { cur, moneyLocale } from "../lib/money"
import { momentParts } from "../lib/localDates"
import { useT } from "../lib/i18n"
import { useAuth } from "../lib/auth"
import { cn } from "../lib/utils"


// kamra.channel_payments.to_collect: Reservation handle_stays, like the requests inbox
const LIST_ROLES = ["Front Desk", "Hotel Admin", "System Manager", "Administrator", "Kamra Agent"]
const SHOWN = 8

// whole amounts, as the drawer and the requests inbox show them
const money = (n: number) =>
  cur() + Number(n || 0).toLocaleString(moneyLocale(), { maximumFractionDigits: 0 })

const PILL: Record<PlatformCollectionState, string> = {
  // a refund is owed to someone, not a debt: the information blue
  refund: "bg-sky-50 text-sky-800 ring-sky-200",
  no_show: "bg-rose-50 text-rose-700 ring-rose-200",
  cancelled: "bg-rose-50 text-rose-700 ring-rose-200",
  arrival: "bg-rose-50 text-rose-700 ring-rose-200",
  now: "bg-amber-50 text-amber-800 ring-amber-200",
  upcoming: "bg-zinc-50 text-zinc-600 ring-zinc-200",
}

/** Today: the channel bookings the hotel collects that still owe money, or
 * that were cancelled in time and are owed back (HR-380). The hotel charges
 * as soon as a booking comes in; urgent ones first, then by arrival. */
export default function PlatformCollections({ refreshKey }: { refreshKey?: unknown }) {
  const { t, tn, lang } = useT()
  const { allowed } = useAuth()
  const canSee = allowed("Reservation", "handle_stays", LIST_ROLES)
  const [data, setData] = useState<Data | null>(null)
  const [all, setAll] = useState(false)

  const load = useCallback(async () => {
    try {
      setData(await call<Data>("kamra.channel_payments.to_collect", { property: getCurrentProperty() }))
    } catch {
      setData(null) // a role without the list just doesn't see the card
    }
  }, [])

  useEffect(() => {
    if (!canSee) return
    load()
    const id = setInterval(load, 60_000)
    return () => clearInterval(id)
  }, [load, refreshKey, canSee])

  if (!data || (data.count === 0 && data.refunds.count === 0)) return null

  const day = (d: string | null) => (d ? momentParts(d, lang)?.date ?? d : "")
  const pill = (r: PlatformCollectionRow) =>
    ({
      refund: t("To refund"),
      no_show: t("Did not show"),
      cancelled: t("Cancelled late"),
      arrival: t("Arrives unpaid"),
      now: t("Charge now"),
      upcoming: t("From {date}", { date: day(r.charge_from) }),
    })[r.state]
  const detail = (r: PlatformCollectionRow) =>
    r.state === "refund"
      ? t("cancelled with credit")
      : r.state === "no_show" || r.state === "cancelled"
        ? t("policy fee")
        : r.collector === "Virtual card"
          ? t("virtual card")
          : tn(r.nights, "arrives {date} · {n} night", "arrives {date} · {n} nights", { date: day(r.check_in_date) })
  const rows = all ? data.rows : data.rows.slice(0, SHOWN)
  const more = data.rows.length - SHOWN

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("Platform collections")}</CardTitle>
        <Wallet className="size-4 text-zinc-400" aria-hidden />
      </CardHeader>
      <CardContent className="pt-1">
        {data.count > 0 && (
          <p className="text-xs text-zinc-500">
            {t("{n} to collect: {amount}. The hotel collects them.", { n: data.count, amount: money(data.total) })}
          </p>
        )}
        {data.refunds.count > 0 && (
          <p className="text-xs text-zinc-500">
            {t("{n} to refund: {amount}.", { n: data.refunds.count, amount: money(data.refunds.total) })}
          </p>
        )}
        <ul className="mt-1 divide-y divide-zinc-100">
          {rows.map((r) => {
            const toFolio = (r.state === "refund" || r.state === "no_show" || r.state === "cancelled") && r.folio
            return (
              <li key={r.name} className="py-2.5">
                <div className="flex items-center justify-between gap-3">
                  <div className="flex min-w-0 items-center gap-2">
                    <span className="truncate text-sm font-medium text-zinc-900">{r.guest_name}</span>
                    <span
                      className={cn(
                        "inline-block whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-semibold ring-1 ring-inset",
                        PILL[r.state],
                      )}
                    >
                      {pill(r)}
                    </span>
                  </div>
                  <span className="shrink-0 text-sm font-semibold tabular-nums text-zinc-900">{money(r.amount)}</span>
                </div>
                <div className="mt-0.5 flex items-center justify-between gap-3">
                  <span className="min-w-0 text-xs text-zinc-500">
                    {[r.channel, detail(r)].filter(Boolean).join(" · ")}
                  </span>
                  <Link
                    to={toFolio
                      ? `/billing/${encodeURIComponent(r.folio ?? "")}`
                      : `/reservations?open=${encodeURIComponent(r.name)}`}
                    className="shrink-0 text-xs font-medium text-brand-700 hover:underline"
                  >
                    {r.state === "refund" ? t("Refund it") : t("Record payment")}
                  </Link>
                </div>
              </li>
            )
          })}
        </ul>
        {more > 0 && !all && (
          <div className="mt-1 flex items-center justify-between gap-3 border-t border-zinc-100 pt-2.5 text-xs">
            <span className="text-zinc-400">{t("{n} more.", { n: more })}</span>
            <button type="button" className="font-medium text-brand-700 hover:underline" onClick={() => setAll(true)}>
              {t("Show all")}
            </button>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
