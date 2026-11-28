import { useCallback, useEffect, useState, type ReactNode } from "react"
import { TrendingUp } from "lucide-react"

import { call, getCurrentProperty } from "../lib/api"
import { serverError } from "../lib/resource"
import { Card, CardContent, CardHeader, CardTitle } from "./ui/card"
import { getLang } from "../lib/dir"
import { t as translate, useUILanguage } from "../lib/i18n"

type Status = "behind" | "on_pace" | "covered" | "no_target" | "past"

interface PacePeriod {
  start: string
  /** exclusive */
  end: string
  capacity: number
  on_books: number
  occupancy_pct: number
  pickup_7: number
  pickup_30: number
  projected_nights: number | null
  projected_pct: number | null
  target_nights: number | null
  target_pct: number | null
  status: Status
}
interface PaceChannel { label: string; kind: "channel" | "source"; on_books: number; pickup_7: number; pickup_30: number }
interface Pace {
  as_of: string
  today: string
  start: string
  grain: "week" | "month"
  rooms: number
  has_targets: boolean
  periods: PacePeriod[]
  /** the overview: the same rows, summed (occupancy, pace and target weighted by nights) */
  summary: {
    capacity: number
    on_books: number
    occupancy_pct: number
    pickup_7: number
    pickup_30: number
    projected_nights: number
    projected_pct: number
    target_nights: number | null
    target_pct: number | null
    status: Record<"behind" | "on_pace" | "covered", number>
  }
  channels: PaceChannel[]
  totals: { on_books: number; pickup_7: number; pickup_30: number }
}

const PILL: Record<Exclude<Status, "past">, [string, string]> = {
  behind: ["Behind", "bg-rose-50 text-rose-700 ring-rose-200"],
  on_pace: ["On pace", "bg-emerald-50 text-emerald-700 ring-emerald-200"],
  covered: ["Target met", "bg-brand-50 text-brand-700 ring-brand-100"],
  no_target: ["No target", "bg-zinc-50 text-zinc-500 ring-zinc-200"],
}
const pillCls = "inline-block whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-semibold ring-1 ring-inset"
const th = "py-2 pr-3 whitespace-nowrap"
const thLast = "py-2 whitespace-nowrap text-right"

const signed = (n: number) => (n > 0 ? `+${n}` : n < 0 ? `−${Math.abs(n)}` : "0")
const tone = (n: number) => (n < 0 ? "text-rose-600" : n > 0 ? "text-zinc-800" : "text-zinc-400")
const pct = (n: number | null) => (n === null ? "—" : `${Math.round(n)}%`)

/** "YYYY-MM-DD" as a local date (never new Date("YYYY-MM-DD"), which is UTC). */
function localDate(iso: string, shiftDays = 0) {
  const [y, m, d] = iso.split("-").map(Number)
  return new Date(y, m - 1, d + shiftDays)
}
function periodLabel(p: PacePeriod, grain: Pace["grain"]) {
  const lang = getLang()
  if (grain === "month") return localDate(p.start).toLocaleDateString(lang, { month: "long", year: "numeric" })
  const opts: Intl.DateTimeFormatOptions = { day: "numeric", month: "short" }
  return `${localDate(p.start).toLocaleDateString(lang, opts)} – ${localDate(p.end, -1).toLocaleDateString(lang, opts)}`
}

function Figure({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <div className="whitespace-nowrap text-[11px] font-medium uppercase tracking-wider text-zinc-400">{label}</div>
      <div className="mt-0.5 text-lg font-semibold tabular-nums text-zinc-800">{children}</div>
    </div>
  )
}

export default function PickupPace() {
  useUILanguage()
  const property = getCurrentProperty()
  const [grain, setGrain] = useState<Pace["grain"]>("week")
  const [start, setStart] = useState<string | null>(null)
  const [data, setData] = useState<Pace | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    call<Pace>("kamra.reports.pickup_pace", { property, grain, ...(start ? { start } : {}) })
      .then((d) => {
        setData(d)
        setError(null)
      })
      .catch((e) => setError(serverError(e)))
  }, [property, grain, start])
  useEffect(load, [load])

  const s = data?.summary
  return (
    <Card>
      <CardHeader className="flex-wrap gap-3">
        <div>
          <CardTitle className="flex items-center gap-2">
            <TrendingUp className="size-4 text-brand-600" aria-hidden="true" />
            {translate("Booking pace")}
          </CardTitle>
          <p className="mt-0.5 text-xs text-zinc-400">
            {translate("Room nights sold for the coming dates, and how many came in over the last 7 and 30 days.")}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <div className="inline-flex overflow-hidden rounded-lg border border-zinc-300 text-sm font-medium">
            {(["week", "month"] as const).map((g) => (
              <button
                key={g}
                type="button"
                aria-pressed={grain === g}
                onClick={() => setGrain(g)}
                className={grain === g ? "bg-brand-600 px-3 py-1.5 text-white" : "bg-white px-3 py-1.5 text-zinc-800"}
              >
                {translate(g === "week" ? "Weeks" : "Months")}
              </button>
            ))}
          </div>
          <label className="ml-1 inline-flex items-center gap-1.5 whitespace-nowrap text-xs text-zinc-400">
            {translate("Starting from")}
            <input
              type="date"
              className="rounded-lg border border-zinc-300 px-2 py-1.5 text-sm text-zinc-800"
              value={start ?? data?.start ?? ""}
              onChange={(e) => e.target.value && setStart(e.target.value)}
            />
          </label>
        </div>
      </CardHeader>
      <CardContent>
        {error && (
          <div className="mb-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
            {translate(error)}
          </div>
        )}
        {data && s && (
          <>
            <h3 className="text-xs font-semibold uppercase tracking-wider text-zinc-500">{translate("At a glance")}</h3>
            <div className="mt-2 grid grid-cols-2 gap-4 border-b border-zinc-100 pb-4 sm:grid-cols-3 lg:grid-cols-6">
              <Figure label={translate("Nights sold")}>{s.on_books}</Figure>
              <Figure label={translate("Occupancy")}>{pct(s.occupancy_pct)}</Figure>
              <Figure label={translate("Last 7 days")}><span className={tone(s.pickup_7)}>{signed(s.pickup_7)}</span></Figure>
              <Figure label={translate("Last 30 days")}><span className={tone(s.pickup_30)}>{signed(s.pickup_30)}</span></Figure>
              <Figure label={translate("At this pace")}>{pct(s.projected_pct)}</Figure>
              <Figure label={translate("Target")}>
                {s.target_pct === null ? <span className="text-zinc-400">{translate("No target")}</span> : pct(s.target_pct)}
              </Figure>
              <div className="col-span-2 sm:col-span-3 lg:col-span-6">
                <div className="whitespace-nowrap text-[11px] font-medium uppercase tracking-wider text-zinc-400">{translate("Status")}</div>
                <div className="mt-1 flex flex-wrap gap-1.5">
                  {(["behind", "on_pace", "covered"] as const).map((k) => (
                    <span key={k} className={`${pillCls} ${PILL[k][1]}`}>
                      {translate(PILL[k][0])} · <span className="tabular-nums">{s.status[k]}</span>
                    </span>
                  ))}
                </div>
              </div>
            </div>

            <div className="mt-2 overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-zinc-200 text-left text-xs font-medium uppercase tracking-wider text-zinc-500">
                    <th className={th}>{translate("Stay")}</th>
                    <th className={th}>{translate("Status")}</th>
                    <th className={`${th} text-right`}>{translate("Sold")}</th>
                    <th className={`${th} text-right`}>{translate("Occupancy")}</th>
                    <th className={`${th} text-right`}>{translate("Last 7 days")}</th>
                    <th className={`${th} text-right`}>{translate("Last 30 days")}</th>
                    <th className={`${th} text-right`}>{translate("At this pace")}</th>
                    <th className={thLast}>{translate("Target")}</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-zinc-100">
                  {data.periods.map((p) => (
                    <tr key={p.start}>
                      <td className="py-2.5 pr-3 font-medium whitespace-nowrap">{periodLabel(p, data.grain)}</td>
                      <td className="py-2.5 pr-3">
                        {p.status === "past" ? (
                          "—"
                        ) : (
                          <span
                            className={`${pillCls} ${PILL[p.status][1]}`}
                            title={
                              p.status === "behind" && p.target_nights !== null && p.projected_nights !== null
                                ? translate("{n} room nights short of the target", {
                                    n: String(Math.ceil(p.target_nights - p.projected_nights)),
                                  })
                                : undefined
                            }
                          >
                            {translate(PILL[p.status][0])}
                          </span>
                        )}
                      </td>
                      <td className="py-2.5 pr-3 text-right tabular-nums whitespace-nowrap">{p.on_books}</td>
                      <td className="py-2.5 pr-3 text-right">
                        <div className="flex items-center justify-end gap-2">
                          <div className="h-1.5 w-16 overflow-hidden rounded-full bg-zinc-100">
                            <div className="h-full rounded-full bg-brand-600" style={{ width: `${Math.min(100, p.occupancy_pct)}%` }} />
                          </div>
                          <span className="w-10 text-right tabular-nums">{pct(p.occupancy_pct)}</span>
                        </div>
                      </td>
                      <td className={`py-2.5 pr-3 text-right tabular-nums font-semibold whitespace-nowrap ${tone(p.pickup_7)}`}>{signed(p.pickup_7)}</td>
                      <td className={`py-2.5 pr-3 text-right tabular-nums whitespace-nowrap ${tone(p.pickup_30)}`}>{signed(p.pickup_30)}</td>
                      <td className="py-2.5 pr-3 text-right tabular-nums whitespace-nowrap">{pct(p.projected_pct)}</td>
                      <td className="py-2.5 text-right tabular-nums whitespace-nowrap text-zinc-500">{pct(p.target_pct)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="mt-3 space-y-1 text-xs text-zinc-500">
              <p>{translate("Sold and occupancy: room nights confirmed today. Last 7 and 30 days: what came in minus what was cancelled or moved to other dates.")}</p>
              <p>{translate("At this pace: the occupancy it would reach if every week brings what the last 7 days brought. The target is each month's occupancy in “Budget vs Actual”.")}</p>
              {!data.has_targets && (
                <p>{translate("Set each month's occupancy target in “Budget vs Actual” to see which dates are behind.")}</p>
              )}
            </div>

            <h3 className="mt-6 text-xs font-semibold uppercase tracking-wider text-zinc-500">{translate("By channel, for these dates")}</h3>
            <div className="overflow-x-auto">
              <table className="mt-1 w-full max-w-xl text-sm">
                <thead>
                  <tr className="border-b border-zinc-200 text-left text-xs font-medium uppercase tracking-wider text-zinc-500">
                    <th className={th}>{translate("Channel")}</th>
                    <th className={`${th} text-right`}>{translate("Sold")}</th>
                    <th className={`${th} text-right`}>{translate("Last 7 days")}</th>
                    <th className={thLast}>{translate("Last 30 days")}</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-zinc-100">
                  {data.channels.map((c) => (
                    <tr key={`${c.kind}:${c.label}`}>
                      <td className="py-2 pr-3 font-medium whitespace-nowrap">{c.kind === "source" ? translate(c.label) : c.label}</td>
                      <td className="py-2 pr-3 text-right tabular-nums">{c.on_books}</td>
                      <td className={`py-2 pr-3 text-right tabular-nums font-semibold ${tone(c.pickup_7)}`}>{signed(c.pickup_7)}</td>
                      <td className={`py-2 text-right tabular-nums ${tone(c.pickup_30)}`}>{signed(c.pickup_30)}</td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr className="border-t border-zinc-200 font-semibold">
                    <td className="py-2 pr-3">{translate("Total")}</td>
                    <td className="py-2 pr-3 text-right tabular-nums">{data.totals.on_books}</td>
                    <td className="py-2 pr-3 text-right tabular-nums">{signed(data.totals.pickup_7)}</td>
                    <td className="py-2 text-right tabular-nums">{signed(data.totals.pickup_30)}</td>
                  </tr>
                </tfoot>
              </table>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}
