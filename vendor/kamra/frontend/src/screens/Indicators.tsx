import { useCallback, useEffect, useState, type ReactNode } from "react"
import { Link } from "react-router-dom"

import { call, getCurrentProperty } from "../lib/api"
import { serverError } from "../lib/resource"
import { useAuth } from "../lib/auth"
import { Button } from "../components/ui/button"
import { Card } from "../components/ui/card"
import { Sheet } from "../components/ui/sheet"
import { cur, moneyLocale } from "../lib/money"
import { getLang } from "../lib/dir"
import { t as translate, tn, useUILanguage } from "../lib/i18n"

// The marketing plan's indicators (HR-379): kamra.indicators.indicators.

interface Channel {
  key: string
  kind: "channel" | "source"
  nights: number
  nights_pct: number | null
  revenue: number
  revenue_pct: number | null
  adr: number | null
  commission: number
  payment_cost: number
  ads_spend: number
  distribution_cost_pct: number | null
  net_adr: number | null
  arrivals: number
  cancelled_pct: number | null
  no_show_pct: number | null
}
type Totals = Omit<Channel, "key" | "kind" | "nights_pct" | "revenue_pct">
interface Review { platform: string; scale: number; score: number | null; count: number | null }
interface Ad { name: string; channel: string; spend: number; sales: number; roas?: number | null }
interface Outside {
  has_inputs: boolean
  booking_page_views: number | null
  booking_reservations: number
  booking_conversion_pct: number | null
  reviews: Review[]
  ads: Ad[]
  payment_costs: { channel: string; amount: number }[]
}
interface Data {
  today: string
  period: string
  window: { from: string; to: string } | null
  rooms: number
  week: { from: string; to: string; occupancy_pct: number; adr: number | null }
  on_books: { days: number; until: string; occupancy_pct: number }[]
  month: null | {
    occupancy_pct: number
    rooms_sold: number
    room_nights_available: number
    adr: number | null
    revpar: number
    direct_nights_pct: number | null
    direct_revenue_pct: number | null
    repeat: number
    reservations: number
    repeat_pct: number | null
    consent_list: number
    consent_new: number
  }
  channels: Channel[]
  totals: Totals | null
  missing_commission: string[]
  lead_time: { market: string | null; reservations: number; avg_days: number }[]
  lead_time_total: { reservations: number; avg_days: number | null } | null
  outside: Outside
}
interface Pace { periods: { pickup_7: number; status: string }[] }

const DASH = "—"
const inr = (n: number) =>
  Number(n || 0).toLocaleString(moneyLocale(), { maximumFractionDigits: 0 })
const money = (n: number | null | undefined) => (n == null ? DASH : `${cur()} ${inr(n)}`)
const pct = (n: number | null | undefined, digits = 0) =>
  n == null
    ? DASH
    : new Intl.NumberFormat(getLang(), { style: "percent", maximumFractionDigits: digits, minimumFractionDigits: digits }).format(n / 100)
const num = (n: number | null | undefined, digits = 0) =>
  n == null ? DASH : n.toLocaleString(getLang(), { maximumFractionDigits: digits })
const day = (iso: string) => {
  const [y, m, d] = iso.split("-").map(Number)
  return new Date(y, m - 1, d)
}
const fmtDay = (iso: string, o: Intl.DateTimeFormatOptions) => day(iso).toLocaleDateString(getLang(), o)
const monthName = (period: string, withYear = true) =>
  fmtDay(`${period}-01`, withYear ? { month: "long", year: "numeric" } : { month: "long" })
const channelName = (c: { key: string; kind: string }) => (c.kind === "source" ? translate(c.key) : c.key)

function shiftMonth(period: string, by: number) {
  const [y, m] = period.split("-").map(Number)
  const d = new Date(y, m - 1 + by, 1)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`
}

// --- pieces (the mockup's markup) ---------------------------------------------

function Tile(props: { label: string; value: ReactNode; sub?: ReactNode; extra?: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col rounded-xl border border-zinc-200 bg-white p-4">
      <span className="whitespace-nowrap text-[13px] font-medium text-zinc-500">{props.label}</span>
      <div className="mt-1.5 whitespace-nowrap text-2xl font-semibold tracking-tight tabular-nums text-zinc-900">{props.value}</div>
      {props.sub != null && <div className="mt-1 whitespace-nowrap text-xs text-zinc-400">{props.sub}</div>}
      {props.extra}
    </div>
  )
}

function Bar({ value }: { value: number | null | undefined }) {
  const w = Math.max(0, Math.min(100, value ?? 0))
  return (
    <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-zinc-100">
      <div className="h-full rounded-full bg-brand-600" style={{ width: `${w}%` }} />
    </div>
  )
}

function Section(props: { title: string; sub?: string; right?: ReactNode; children: ReactNode }) {
  return (
    <Card>
      <div className="flex flex-wrap items-center justify-between gap-3 px-5 pt-4">
        <div>
          <h2 className="whitespace-nowrap text-sm font-semibold tracking-wide text-zinc-700">{props.title}</h2>
          {props.sub && <p className="mt-0.5 text-xs text-zinc-500">{props.sub}</p>}
        </div>
        {props.right}
      </div>
      <div className="px-5 pb-5 pt-4">{props.children}</div>
    </Card>
  )
}

const H3 = ({ children }: { children: ReactNode }) => (
  <h3 className="mt-6 mb-1 whitespace-nowrap text-xs font-semibold uppercase tracking-wider text-zinc-500">{children}</h3>
)
const TH = "py-2 pr-3 whitespace-nowrap"
const TD = "py-2.5 pr-3 whitespace-nowrap tabular-nums"
const Pill = () => (
  <span className="inline-block whitespace-nowrap rounded-full bg-amber-50 px-2 py-0.5 text-xs font-semibold text-amber-800 ring-1 ring-inset ring-amber-200">
    {translate("% missing")}
  </span>
)
const Muted = ({ children }: { children: ReactNode }) => <span className="text-zinc-400">{children}</span>

function Datum({ k, v }: { k: ReactNode; v: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 py-1.5">
      <span className="whitespace-nowrap text-zinc-600">{k}</span>
      <span className="whitespace-nowrap font-semibold tabular-nums">{v}</span>
    </div>
  )
}
const Where = ({ children }: { children: ReactNode }) => (
  <p className="mt-3 border-t border-zinc-100 pt-2 text-xs text-zinc-500">{children}</p>
)
function Box({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col rounded-xl border border-zinc-200 bg-white p-4">
      <span className="whitespace-nowrap text-[13px] font-semibold text-zinc-700">{title}</span>
      <div className="mt-2 flex-1 divide-y divide-zinc-100 text-sm">{children}</div>
    </div>
  )
}

// --- this week and what's ahead -------------------------------------------------

function PaceTile() {
  const property = getCurrentProperty()
  const [pace, setPace] = useState<Pace | null>(null)
  useEffect(() => {
    call<Pace>("kamra.reports.pickup_pace", { property, grain: "week" })
      .then(setPace)
      .catch(() => setPace(null))
  }, [property])
  const link = (
    <Link className="mt-1 whitespace-nowrap text-xs font-medium text-brand-700 hover:underline" to="/revenue-reports">
      {translate("See the pace →")}
    </Link>
  )
  if (!pace?.periods) return <Tile label={translate("Pace of bookings")} value={DASH} extra={link} />
  const first = pace.periods.slice(0, 8)
  const sum = first.reduce((s, p) => s + (p.pickup_7 || 0), 0)
  const behind = first.filter((p) => p.status === "behind").length
  let value = translate("+{n} room nights", { n: num(Math.abs(sum)) })
  if (sum < 0) value = value.replace(/^\+/, "−")
  const sub = behind
    ? <span className="text-rose-600">{tn(behind, "1 week behind", "{n} weeks behind")}</span>
    : translate("None behind")
  return <Tile label={translate("Pace of bookings")} value={value} sub={sub} extra={link} />
}

function Week({ data }: { data: Data }) {
  return (
    <Section
      title={translate("This week and what's ahead")}
      sub={translate("Today, {date}. Checked every Monday.", {
        date: fmtDay(data.today, { weekday: "long", day: "numeric", month: "long" }),
      })}
    >
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <Tile label={translate("Occupancy")} value={pct(data.week.occupancy_pct)} sub={translate("last 7 days")}
          extra={<Bar value={data.week.occupancy_pct} />} />
        <Tile label={translate("Average rate")} value={money(data.week.adr)} sub={translate("last 7 days")} />
        <PaceTile />
        {data.on_books.map((w) => (
          <Tile key={w.days} label={translate("Booked, next {n} days", { n: w.days })} value={pct(w.occupancy_pct)}
            sub={translate("until {date}", { date: fmtDay(w.until, { day: "numeric", month: "short" }) })}
            extra={<Bar value={w.occupancy_pct} />} />
        ))}
      </div>
      <p className="mt-3 text-xs text-zinc-500">
        {translate("Pace of bookings: room nights booked in the last 7 days for the next 8 weeks, less the cancelled ones. Booked: what is already sold for those dates, as on the Revenue dashboard.")}
      </p>
    </Section>
  )
}

// --- the month --------------------------------------------------------------------

function ChannelTable({ data }: { data: Data }) {
  const missing = new Set(data.missing_commission)
  const tot = data.totals
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-zinc-200 text-left text-xs font-medium uppercase tracking-wider text-zinc-500">
            <th className={`${TH} sticky left-0 bg-white`}>{translate("Channel")}</th>
            <th className={`${TH} text-right`}>{translate("Nights")}</th>
            <th className={`${TH} text-right`}>{translate("Revenue")}</th>
            <th className={`${TH} text-right`}>{translate("Average rate")}</th>
            <th className={`${TH} text-right`}>{translate("Commission")}</th>
            <th className={`${TH} text-right`}>{translate("Payment and ads")}</th>
            <th className={`${TH} text-right`}>{translate("Distribution cost")}</th>
            <th className={`${TH} text-right`}>{translate("Net rate")}</th>
            <th className={`${TH} text-right`}>{translate("Cancellations")}</th>
            <th className="py-2 whitespace-nowrap text-right">{translate("No-shows")}</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-100">
          {data.channels.map((c) => (
            <tr key={c.key}>
              <td className="py-2.5 pr-4 font-medium whitespace-nowrap sticky left-0 bg-white">{channelName(c)}</td>
              <td className={`${TD} text-right`}>{num(c.nights)} <Muted>· {pct(c.nights_pct)}</Muted></td>
              <td className={`${TD} text-right`}>{inr(c.revenue)} <Muted>· {pct(c.revenue_pct)}</Muted></td>
              <td className={`${TD} text-right`}>{c.adr == null ? DASH : inr(c.adr)}</td>
              <td className={`${TD} text-right`}>{missing.has(c.key) ? <Pill /> : inr(c.commission)}</td>
              <td className={`${TD} text-right`}>{inr(c.payment_cost + c.ads_spend)}</td>
              <td className={`${TD} text-right font-semibold`}>{pct(c.distribution_cost_pct, 1)}</td>
              <td className={`${TD} text-right font-semibold`}>{c.net_adr == null ? DASH : inr(c.net_adr)}</td>
              <td className={`${TD} text-right`}>{pct(c.cancelled_pct)}</td>
              <td className="py-2.5 whitespace-nowrap tabular-nums text-right">{pct(c.no_show_pct)}</td>
            </tr>
          ))}
        </tbody>
        {tot && (
          <tfoot>
            <tr className="border-t border-zinc-200 font-semibold">
              <td className="py-2 pr-4 sticky left-0 bg-white">{translate("Total")}</td>
              <td className={`${TD} text-right`}>{num(tot.nights)}</td>
              <td className={`${TD} text-right`}>{inr(tot.revenue)}</td>
              <td className={`${TD} text-right`}>{tot.adr == null ? DASH : inr(tot.adr)}</td>
              <td className={`${TD} text-right`}>{inr(tot.commission)}</td>
              <td className={`${TD} text-right`}>{inr(tot.payment_cost + tot.ads_spend)}</td>
              <td className={`${TD} text-right`}>{pct(tot.distribution_cost_pct, 1)}</td>
              <td className={`${TD} text-right`}>{tot.net_adr == null ? DASH : inr(tot.net_adr)}</td>
              <td className={`${TD} text-right`}>{pct(tot.cancelled_pct)}</td>
              <td className="py-2 text-right tabular-nums">{pct(tot.no_show_pct)}</td>
            </tr>
          </tfoot>
        )}
      </table>
    </div>
  )
}

function LeadTime({ data }: { data: Data }) {
  let names: Intl.DisplayNames | null = null
  try {
    names = new Intl.DisplayNames([getLang()], { type: "region" })
  } catch {
    names = null
  }
  const country = (code: string | null) => (code ? names?.of(code) ?? code : translate("No data"))
  const days = (n: number | null | undefined) => (n == null ? DASH : translate("{n} days", { n }))
  return (
    <div className="overflow-x-auto">
      <table className="w-full max-w-md text-sm">
        <thead>
          <tr className="border-b border-zinc-200 text-left text-xs font-medium uppercase tracking-wider text-zinc-500">
            <th className={TH}>{translate("Country")}</th>
            <th className={`${TH} text-right`}>{translate("Bookings")}</th>
            <th className="py-2 whitespace-nowrap text-right">{translate("Lead time")}</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-100">
          {data.lead_time.map((r) => (
            <tr key={r.market ?? "-"}>
              <td className="py-2.5 pr-3 font-medium whitespace-nowrap">{country(r.market)}</td>
              <td className={`${TD} text-right`}>{num(r.reservations)}</td>
              <td className="py-2.5 whitespace-nowrap tabular-nums text-right">{days(r.avg_days)}</td>
            </tr>
          ))}
        </tbody>
        {data.lead_time_total && (
          <tfoot>
            <tr className="border-t border-zinc-200 font-semibold">
              <td className="py-2 pr-3">{translate("Total")}</td>
              <td className={`${TD} text-right`}>{num(data.lead_time_total.reservations)}</td>
              <td className="py-2 whitespace-nowrap text-right tabular-nums">{days(data.lead_time_total.avg_days)}</td>
            </tr>
          </tfoot>
        )}
      </table>
    </div>
  )
}

function Month({ data, period, setPeriod }: { data: Data; period: string; setPeriod: (f: (p: string) => string) => void }) {
  const m = data.month
  const w = data.window
  const from = w ? fmtDay(w.from, { day: "numeric" }) : ""
  const to = w ? fmtDay(w.to, { day: "numeric", month: "long" }) : ""
  const sub = !w
    ? undefined
    : data.period === data.today.slice(0, 7)
      ? translate("From {from} to {to} (up to yesterday). Checked once a month.", { from, to })
      : translate("From {from} to {to}. Checked once a month.", { from, to })
  const nav = (
    <div className="flex items-center gap-1.5">
      <Button variant="outline" className="px-2.5" aria-label={translate("Previous month")} onClick={() => setPeriod((p) => shiftMonth(p, -1))}>‹</Button>
      <span className="min-w-28 whitespace-nowrap text-center text-sm font-medium">{monthName(period)}</span>
      <Button variant="outline" className="px-2.5" aria-label={translate("Next month")} onClick={() => setPeriod((p) => shiftMonth(p, 1))}>›</Button>
    </div>
  )
  const list = new Intl.ListFormat(getLang(), { type: "conjunction" })
  return (
    <Section title={translate("The month")} sub={sub} right={nav}>
      {!m ? (
        <p className="text-sm text-zinc-500">{translate("This month has not started yet.")}</p>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            <Tile label={translate("Occupancy")} value={pct(m.occupancy_pct)}
              sub={translate("{a} of {b} room nights", { a: num(m.rooms_sold), b: num(m.room_nights_available) })}
              extra={<Bar value={m.occupancy_pct} />} />
            <Tile label={translate("Average rate")} value={money(m.adr)} sub={translate("per room night sold")} />
            <Tile label={translate("RevPAR")} value={money(m.revpar)} sub={translate("per room")} />
            <Tile label={translate("Direct sales")} value={pct(m.direct_nights_pct)} sub={translate("of room nights")} />
            <Tile label={translate("Returning guests")} value={pct(m.repeat_pct)}
              sub={translate("{a} of {b} bookings", { a: num(m.repeat), b: num(m.reservations) })} />
            <Tile label={translate("Contact list")} value={num(m.consent_list)}
              sub={`${translate("with consent")} · +${num(m.consent_new)}`} />
          </div>
          <H3>{translate("By channel")}</H3>
          {data.missing_commission.length > 0 && (
            <div className="mb-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
              {translate("The commission % of {channels} is missing: until it is set, its cost and net rate show «—».", {
                channels: list.format(data.missing_commission),
              })}{" "}
              <Link className="font-medium underline" to="/travel-agents">{translate("Set it in Travel Agents →")}</Link>
            </div>
          )}
          <ChannelTable data={data} />
          <div className="mt-3 space-y-1 text-xs text-zinc-500">
            <p>{translate("Room nights and revenue ({cur}) of the stays from {from} to {to}: before tax on website bookings; platform and manual ones as they came. Commission: what the platform sent or, if not, its % in Travel Agents.", { cur: cur(), from, to })}</p>
            <p>{translate("Payment and ads: from the month's figures below. Distribution cost: commission, payment and ads over the channel's revenue. Net rate: the average rate less those costs. Cancellations and no-shows: over the bookings arriving on those dates.")}</p>
          </div>
          <H3>{translate("Booking lead time by market")}</H3>
          <LeadTime data={data} />
          <p className="mt-3 text-xs text-zinc-500">
            {translate("The country comes from the guest's phone; lead time is the average days between booking and arrival.")}
          </p>
        </>
      )}
    </Section>
  )
}

// --- outside Kamra ------------------------------------------------------------------

function OutsideKamra({ data, onEnter }: { data: Data; onEnter: (() => void) | null }) {
  const o = data.outside
  const score = (n: number) => n.toLocaleString(getLang(), { maximumFractionDigits: 1 })
  const button = onEnter && (
    <Button className="whitespace-nowrap" onClick={onEnter}>
      {translate("Enter {month}'s figures", { month: monthName(data.period, false) })}
    </Button>
  )
  return (
    <Section title={translate("Outside Kamra")} sub={translate("What Booking, reviews and ads give. Entered once a month.")} right={button}>
      <div className="grid gap-3 lg:grid-cols-3">
        <Box title={translate("Booking: views and conversion")}>
          <Datum k={translate("Page views")} v={num(o.booking_page_views)} />
          <Datum k={translate("Booking bookings")} v={num(o.booking_reservations)} />
          <Datum k={translate("Conversion")} v={pct(o.booking_conversion_pct, 1)} />
          <Where>{translate("Where to look: Booking extranet → Analytics. Kamra counts the bookings.")}</Where>
        </Box>
        <Box title={translate("Rating and number of reviews")}>
          {o.reviews.map((r) => (
            <Datum key={r.platform} k={r.platform} v={r.score != null || r.count != null
              ? <>{r.score != null ? translate("{score} of {scale}", { score: score(r.score), scale: r.scale }) : DASH}{" "}
                <span className="font-normal text-zinc-400">· {num(r.count)}</span></>
              : <span className="font-normal text-zinc-400">{translate("not entered")}</span>} />
          ))}
          <Where>{translate("Where to look: each platform.")}</Where>
        </Box>
        <Box title={translate("Ads (ROAS)")}>
          {o.ads.length === 0 && <Datum k={<Muted>{translate("not entered")}</Muted>} v="" />}
          {o.ads.map((a, i) => (
            <div key={i} className="py-1.5">
              <div className="flex items-center justify-between gap-3">
                <span className="whitespace-nowrap text-zinc-600">{a.name}</span>
                <span className="whitespace-nowrap font-semibold tabular-nums">
                  {a.roas == null ? DASH : translate("ROAS {n}", { n: num(a.roas, 1) })}
                </span>
              </div>
              <div className="whitespace-nowrap text-xs text-zinc-400 tabular-nums">
                {translate("Spend {a}", { a: money(a.spend) })} · {translate("sales {b}", { b: money(a.sales) })}
              </div>
            </div>
          ))}
          <Where>{translate("Where to look: each ad's dashboard; there, every week while it runs.")}</Where>
        </Box>
      </div>
    </Section>
  )
}

// --- the month's figures form (Indicator Month) ---------------------------------------

type Draft = {
  views: string
  reviews: Record<string, { score: string; count: string }>
  ads: { name: string; channel: string; spend: string; sales: string }[]
  pay: Record<string, string>
}
const str = (n: number | null | undefined) => (n == null ? "" : String(n))
const parse = (s: string) => (s.trim() === "" ? null : Number(s.trim().replace(",", ".")))
const INPUT = "rounded-lg border border-zinc-300 px-2 py-1 text-right text-sm"
const GRID = { gridTemplateColumns: "1fr 5rem 5rem" }

function FiguresSheet({ data, onClose, onSaved }: { data: Data; onClose: () => void; onSaved: () => void }) {
  const property = getCurrentProperty()
  const o = data.outside
  // where a cost can go: the website and the month's platforms
  const channels = ["Website", ...data.channels.filter((c) => c.kind === "channel").map((c) => c.key)]
  for (const x of [...o.ads, ...o.payment_costs]) if (!channels.includes(x.channel)) channels.push(x.channel)
  const label = (k: string) => (k === "Website" ? translate("Website") : channelName(data.channels.find((c) => c.key === k) ?? { key: k, kind: "channel" }))
  const [draft, setDraft] = useState<Draft>(() => ({
    views: str(o.booking_page_views),
    reviews: Object.fromEntries(o.reviews.map((r) => [r.platform, { score: str(r.score), count: str(r.count) }])),
    ads: o.ads.length
      ? o.ads.map((a) => ({ name: a.name, channel: a.channel, spend: str(a.spend), sales: str(a.sales) }))
      : [{ name: "", channel: "Website", spend: "", sales: "" }],
    pay: Object.fromEntries(channels.map((c) => [c, str(o.payment_costs.find((p) => p.channel === c)?.amount)])),
  }))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      await call("kamra.indicators.save_month_inputs", {
        property,
        period: data.period,
        inputs: {
          booking_page_views: parse(draft.views),
          reviews: o.reviews.map((r) => ({
            platform: r.platform,
            score: parse(draft.reviews[r.platform]?.score ?? ""),
            count: parse(draft.reviews[r.platform]?.count ?? ""),
          })),
          ads: draft.ads.map((a) => ({ name: a.name, channel: a.channel, spend: parse(a.spend), sales: parse(a.sales) })),
          payment_costs: channels.map((c) => ({ channel: c, amount: parse(draft.pay[c] ?? "") })),
        },
      })
      onSaved()
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }
  const group = (title: string, body: ReactNode) => (
    <div className="mb-5">
      <h3 className="mb-1 whitespace-nowrap text-xs font-semibold uppercase tracking-wider text-zinc-500">{title}</h3>
      {body}
    </div>
  )
  const setAd = (i: number, k: keyof Draft["ads"][number], v: string) =>
    setDraft((d) => ({ ...d, ads: d.ads.map((a, j) => (j === i ? { ...a, [k]: v } : a)) }))
  return (
    <Sheet
      title={translate("{month} figures", { month: monthName(data.period) })}
      description={translate("What is not in Kamra. Entered once a month.")}
      onClose={onClose}
      footer={
        <>
          <Button variant="outline" onClick={onClose} disabled={busy}>{translate("Cancel")}</Button>
          <Button onClick={save} disabled={busy}>{translate("Save")}</Button>
        </>
      }
    >
      {error && (
        <div className="mb-4 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">{translate(error)}</div>
      )}
      {group("Booking", (
        <label className="flex items-center justify-between gap-3 py-1.5 text-sm">
          <span className="whitespace-nowrap text-zinc-700">
            {translate("Page views")} <span className="text-xs text-zinc-400">{translate("in the month")}</span>
          </span>
          <input className={`w-28 ${INPUT}`} inputMode="numeric" value={draft.views}
            onChange={(e) => setDraft((d) => ({ ...d, views: e.target.value }))} />
        </label>
      ))}
      {group(translate("Rating and number of reviews"), o.reviews.map((r) => (
        <div key={r.platform} className="grid items-center gap-2 py-1 text-sm" style={GRID}>
          <span className="whitespace-nowrap text-zinc-700">
            {r.platform} <span className="text-xs text-zinc-400">{translate("of {scale}", { scale: r.scale })}</span>
          </span>
          <input className={INPUT} inputMode="decimal" placeholder={translate("Rating")} aria-label={`${r.platform} · ${translate("Rating")}`}
            value={draft.reviews[r.platform]?.score ?? ""}
            onChange={(e) => setDraft((d) => ({ ...d, reviews: { ...d.reviews, [r.platform]: { ...d.reviews[r.platform], score: e.target.value } } }))} />
          <input className={INPUT} inputMode="numeric" placeholder={translate("Number")} aria-label={`${r.platform} · ${translate("Number")}`}
            value={draft.reviews[r.platform]?.count ?? ""}
            onChange={(e) => setDraft((d) => ({ ...d, reviews: { ...d.reviews, [r.platform]: { ...d.reviews[r.platform], count: e.target.value } } }))} />
        </div>
      )))}
      {group(`${translate("Ads")} (${cur()})`, (
        <>
          <div className="grid gap-2 pb-1 text-xs text-zinc-400" style={GRID}>
            <span>{translate("Name and channel")}</span>
            <span className="text-right">{translate("Spend")}</span>
            <span className="text-right">{translate("Sales")}</span>
          </div>
          {draft.ads.map((a, i) => (
            <div key={i} className="mb-2 rounded-lg border border-zinc-200 p-2">
              <input className="w-full rounded-lg border border-zinc-300 px-2 py-1 text-sm" value={a.name}
                aria-label={translate("Name and channel")} onChange={(e) => setAd(i, "name", e.target.value)} />
              <div className="mt-2 grid gap-2 text-sm" style={GRID}>
                <select className="rounded-lg border border-zinc-300 px-2 py-1 text-sm" value={a.channel}
                  aria-label={translate("Channel")} onChange={(e) => setAd(i, "channel", e.target.value)}>
                  {channels.map((c) => <option key={c} value={c}>{label(c)}</option>)}
                </select>
                <input className={INPUT} inputMode="decimal" aria-label={translate("Spend")} value={a.spend}
                  onChange={(e) => setAd(i, "spend", e.target.value)} />
                <input className={INPUT} inputMode="decimal" aria-label={translate("Sales")} value={a.sales}
                  onChange={(e) => setAd(i, "sales", e.target.value)} />
              </div>
            </div>
          ))}
          <button type="button" className="mt-1 whitespace-nowrap text-sm font-medium text-brand-700"
            onClick={() => setDraft((d) => ({ ...d, ads: [...d.ads, { name: "", channel: "Website", spend: "", sales: "" }] }))}>
            {translate("+ Add ad")}
          </button>
        </>
      ))}
      {group(`${translate("Payment cost by channel")} (${cur()})`, channels.map((c) => (
        <label key={c} className="flex items-center justify-between gap-3 py-1.5 text-sm">
          <span className="whitespace-nowrap text-zinc-700">{label(c)}</span>
          <input className={`w-28 ${INPUT}`} inputMode="decimal" value={draft.pay[c] ?? ""}
            onChange={(e) => setDraft((d) => ({ ...d, pay: { ...d.pay, [c]: e.target.value } }))} />
        </label>
      )))}
    </Sheet>
  )
}

// ---------------------------------------------------------------------------------------

export default function Indicators() {
  useUILanguage()
  const { allowed } = useAuth()
  const property = getCurrentProperty()
  const [period, setPeriod] = useState<string | null>(null)
  const [data, setData] = useState<Data | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [sheet, setSheet] = useState(false)

  const load = useCallback(() => {
    setError(null)
    call<Data>("kamra.indicators.indicators", { property, ...(period ? { period } : {}) })
      .then((d) => {
        setData(d)
        if (!period) setPeriod(d.period)
      })
      .catch((e) => setError(serverError(e)))
  }, [property, period])
  useEffect(load, [load])

  const canEnter = allowed("Revenue Budget", "set_budget")
  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">{translate("Indicators")}</h1>
        <p className="mt-0.5 text-sm text-zinc-500">{translate("The marketing plan's indicators on one page.")}</p>
      </header>
      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">{translate(error)}</div>
      )}
      {!data && !error && <p className="text-sm text-zinc-400">{translate("Loading…")}</p>}
      {data && (
        <>
          <Week data={data} />
          <Month data={data} period={period ?? data.period} setPeriod={(f) => setPeriod((p) => f(p ?? data.period))} />
          <OutsideKamra data={data} onEnter={canEnter ? () => setSheet(true) : null} />
          {sheet && <FiguresSheet data={data} onClose={() => setSheet(false)} onSaved={() => { setSheet(false); load() }} />}
        </>
      )}
    </div>
  )
}
