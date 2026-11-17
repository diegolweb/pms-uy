/** Stay restrictions: stop sell, closed to arrival / departure and the
 *  minimum and maximum stay, by date, room type and rate plan. The booking
 *  page, the front desk and the channel managers all read these same rows,
 *  so a rule set here holds everywhere. A batch of rows saves in one go. */
import { useCallback, useEffect, useMemo, useState } from "react"
import { Ban, Pencil, Plus, Trash2, X } from "lucide-react"
import { call, getCurrentProperty } from "../lib/api"
import { listResource, serverError } from "../lib/resource"
import { Badge } from "../components/ui/badge"
import { Button } from "../components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card"
import { useT, t as translate, uiDateLocale } from "../lib/i18n"

interface Restriction {
  name?: string
  room_type: string
  rate_plan: string
  date_from: string
  date_to: string
  days_of_week: string
  stop_sell: number
  closed_to_arrival: number
  closed_to_departure: number
  min_stay_arrival: number
  min_stay_through: number
  max_stay: number
  note: string
  disabled: number
}

interface Option {
  name: string
  label: string
}

const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const
const FLAGS = ["stop_sell", "closed_to_arrival", "closed_to_departure"] as const
const NUMBERS = ["min_stay_arrival", "min_stay_through", "max_stay"] as const
const inputCls =
  "rounded-lg border border-zinc-300 bg-white px-2 py-1.5 text-sm focus:outline-2 focus:outline-offset-1 focus:outline-brand-600"

const todayIso = () => {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`
}

const blank = (): Restriction => ({
  room_type: "",
  rate_plan: "",
  date_from: todayIso(),
  date_to: todayIso(),
  days_of_week: "",
  stop_sell: 0,
  closed_to_arrival: 0,
  closed_to_departure: 0,
  min_stay_arrival: 0,
  min_stay_through: 0,
  max_stay: 0,
  note: "",
  disabled: 0,
})

const normalize = (r: Partial<Restriction> & { name?: string }): Restriction => ({
  ...blank(),
  ...Object.fromEntries(Object.entries(r).filter(([, v]) => v !== null && v !== undefined)),
})

/** "fri, sat" -> {"fri", "sat"}; blank means every day. */
const daySet = (s: string) => new Set(s.split(",").map((d) => d.trim().toLowerCase().slice(0, 3)).filter(Boolean))

/** The short weekday name in the UI language, Monday first. */
const dayLabel = (i: number) =>
  new Date(2024, 0, 1 + i).toLocaleDateString(uiDateLocale(undefined), { weekday: "short" })

export default function ChannelRestrictions() {
  const { t } = useT()
  const property = getCurrentProperty()
  const [rows, setRows] = useState<Restriction[] | null>(null)
  const [roomTypes, setRoomTypes] = useState<Option[]>([])
  const [ratePlans, setRatePlans] = useState<Option[]>([])
  const [filter, setFilter] = useState({ room_type: "", date_from: todayIso(), date_to: "" })
  const [draft, setDraft] = useState<Restriction[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [deleting, setDeleting] = useState<string | null>(null)

  useEffect(() => {
    listResource("Room Type", { fields: ["name", "room_type_name"], filters: [["property", "=", property]], orderBy: "room_type_name asc" })
      .then((r) => setRoomTypes(r.map((x) => ({ name: x.name, label: String(x.room_type_name ?? x.name) }))))
      .catch((e) => setErr(serverError(e)))
    listResource("Rate Plan", { fields: ["name", "rate_plan_name", "is_default"], filters: [["property", "=", property], ["disabled", "=", 0]], orderBy: "rate_plan_name asc" })
      .then((r) => setRatePlans(r.map((x) => ({ name: x.name, label: String(x.rate_plan_name ?? x.name) + (x.is_default ? ` (${translate("default")})` : "") }))))
      .catch((e) => setErr(serverError(e)))
  }, [property])

  const load = useCallback(() => {
    call<Restriction[]>("kamra.channel_restrictions.list_restrictions", {
      property,
      date_from: filter.date_from || undefined,
      date_to: filter.date_to || undefined,
      room_type: filter.room_type || undefined,
    })
      .then((r) => {
        setRows(r.map(normalize))
        setErr(null)
      })
      .catch((e) => setErr(serverError(e)))
  }, [property, filter])
  useEffect(load, [load])

  const labelOf = useMemo(() => {
    const m = new Map<string, string>()
    for (const o of [...roomTypes, ...ratePlans]) m.set(o.name, o.label)
    return (name: string) => m.get(name) ?? name
  }, [roomTypes, ratePlans])

  const save = (batch: Restriction[], done: (n: number) => string) => {
    setBusy(true)
    call<{ saved: string[] }>("kamra.channel_restrictions.save_restrictions", { property, rows: batch })
      .then((out) => {
        setDraft(null)
        setErr(null)
        setNotice(done(out.saved.length))
        load()
      })
      .catch((e) => setErr(serverError(e)))
      .finally(() => setBusy(false))
  }

  const remove = (name: string) => {
    setBusy(true)
    call<{ deleted: number }>("kamra.channel_restrictions.delete_restrictions", { property, names: [name] })
      .then(() => {
        setDeleting(null)
        setNotice(translate("Restriction deleted."))
        load()
      })
      .catch((e) => setErr(serverError(e)))
      .finally(() => setBusy(false))
  }

  const tags = (r: Restriction) => {
    const out: { text: string; tone: "rose" | "amber" | "sky" | "indigo" }[] = []
    if (r.stop_sell) out.push({ text: t("Stop sell"), tone: "rose" })
    if (r.closed_to_arrival) out.push({ text: t("Closed to arrival"), tone: "amber" })
    if (r.closed_to_departure) out.push({ text: t("Closed to departure"), tone: "amber" })
    if (r.min_stay_arrival > 1) out.push({ text: t("Min. stay on arrival: {n} nights", { n: r.min_stay_arrival }), tone: "sky" })
    if (r.min_stay_through > 1) out.push({ text: t("Min. stay through: {n} nights", { n: r.min_stay_through }), tone: "sky" })
    if (r.max_stay > 0) out.push({ text: t("Max. stay: {n} nights", { n: r.max_stay }), tone: "indigo" })
    return out
  }

  const days = (r: Restriction) => {
    const set = daySet(r.days_of_week)
    if (!set.size) return t("Every day")
    return DAYS.map((d, i) => (set.has(d) ? dayLabel(i) : null)).filter(Boolean).join(", ")
  }

  const setRow = (i: number, patch: Partial<Restriction>) =>
    setDraft((d) => (d ? d.map((r, j) => (j === i ? { ...r, ...patch } : r)) : d))

  const toggleDay = (i: number, day: string) => {
    if (!draft) return
    const set = daySet(draft[i].days_of_week)
    if (set.has(day)) set.delete(day)
    else set.add(day)
    setRow(i, { days_of_week: DAYS.filter((d) => set.has(d)).join(", ") })
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-lg font-semibold text-zinc-900">
            <Ban className="size-5 text-zinc-500" aria-hidden />
            {t("Stay restrictions")}
          </h1>
          <p className="text-sm text-zinc-500">
            {t("Stop sell, closed arrival or departure, and minimum and maximum stay, by date. The booking page, front desk and channels all follow them.")}
          </p>
        </div>
        {!draft && (
          <Button onClick={() => { setNotice(null); setDraft([blank()]) }}>
            <Plus className="me-1 size-4" aria-hidden />
            {t("New batch")}
          </Button>
        )}
      </div>

      {err && <p className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">{translate(err)}</p>}
      {notice && <p className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">{notice}</p>}

      {draft && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">{draft.some((r) => r.name) ? t("Edit restriction") : t("New batch of restrictions")}</CardTitle>
            <p className="text-sm text-zinc-500">{t("Each row is one restriction. All rows are saved together: if one is wrong, none is saved.")}</p>
          </CardHeader>
          <CardContent className="space-y-3">
            {draft.map((r, i) => (
              <div key={i} className="space-y-3 rounded-lg border border-zinc-200 p-3">
                <div className="flex flex-wrap items-end gap-3">
                  <label className="grid gap-1 text-xs text-zinc-600">
                    {t("Room type")}
                    <select className={inputCls} value={r.room_type} onChange={(e) => setRow(i, { room_type: e.target.value })}>
                      <option value="">{t("All room types")}</option>
                      {roomTypes.map((o) => <option key={o.name} value={o.name}>{o.label}</option>)}
                    </select>
                  </label>
                  <label className="grid gap-1 text-xs text-zinc-600">
                    {t("Rate plan")}
                    <select className={inputCls} value={r.rate_plan} onChange={(e) => setRow(i, { rate_plan: e.target.value })}>
                      <option value="">{t("All rate plans")}</option>
                      {ratePlans.map((o) => <option key={o.name} value={o.name}>{o.label}</option>)}
                    </select>
                  </label>
                  <label className="grid gap-1 text-xs text-zinc-600">
                    {t("From")}
                    <input type="date" className={inputCls} value={r.date_from} onChange={(e) => setRow(i, { date_from: e.target.value })} />
                  </label>
                  <label className="grid gap-1 text-xs text-zinc-600">
                    {t("To")}
                    <input type="date" className={inputCls} value={r.date_to} onChange={(e) => setRow(i, { date_to: e.target.value })} />
                  </label>
                  {draft.length > 1 && (
                    <Button variant="ghost" aria-label={t("Remove row")} onClick={() => setDraft(draft.filter((_, j) => j !== i))}>
                      <X className="size-4" aria-hidden />
                    </Button>
                  )}
                </div>
                <div className="flex flex-wrap items-center gap-1.5 text-xs text-zinc-600">
                  <span className="me-1">{t("Days")}</span>
                  {DAYS.map((d, k) => {
                    const on = daySet(r.days_of_week).has(d)
                    return (
                      <button
                        key={d}
                        type="button"
                        aria-pressed={on}
                        onClick={() => toggleDay(i, d)}
                        className={on ? "rounded-md bg-brand-600 px-2 py-1 text-white" : "rounded-md border border-zinc-300 px-2 py-1 text-zinc-700 hover:bg-zinc-50"}
                      >
                        {dayLabel(k)}
                      </button>
                    )
                  })}
                  <span className="ms-1 text-zinc-400">{daySet(r.days_of_week).size ? "" : t("None selected: every day.")}</span>
                </div>
                <div className="flex flex-wrap items-end gap-4">
                  {FLAGS.map((f) => (
                    <label key={f} className="inline-flex items-center gap-1.5 text-sm text-zinc-700">
                      <input type="checkbox" checked={!!r[f]} onChange={(e) => setRow(i, { [f]: e.target.checked ? 1 : 0 })} />
                      {f === "stop_sell" ? t("Stop sell") : f === "closed_to_arrival" ? t("Closed to arrival") : t("Closed to departure")}
                    </label>
                  ))}
                  {NUMBERS.map((f) => (
                    <label key={f} className="grid gap-1 text-xs text-zinc-600">
                      {f === "min_stay_arrival" ? t("Min. stay on arrival") : f === "min_stay_through" ? t("Min. stay through") : t("Max. stay")}
                      <input
                        type="number"
                        min={0}
                        className={`${inputCls} w-24`}
                        value={r[f] || ""}
                        placeholder="—"
                        onChange={(e) => setRow(i, { [f]: Math.max(0, Number(e.target.value) || 0) })}
                      />
                    </label>
                  ))}
                  <label className="grid flex-1 gap-1 text-xs text-zinc-600">
                    {t("Note")}
                    <input className={inputCls} value={r.note} onChange={(e) => setRow(i, { note: e.target.value })} />
                  </label>
                </div>
              </div>
            ))}
            <div className="flex flex-wrap items-center justify-between gap-2">
              {draft.some((r) => r.name) ? <span /> : (
                <Button variant="outline" onClick={() => setDraft([...draft, { ...draft[draft.length - 1], name: undefined }])}>
                  <Plus className="me-1 size-4" aria-hidden />
                  {t("Add row")}
                </Button>
              )}
              <div className="flex gap-2">
                <Button variant="ghost" onClick={() => setDraft(null)} disabled={busy}>{t("Cancel")}</Button>
                <Button onClick={() => save(draft, (n) => t("{n} restriction(s) saved.", { n }))} disabled={busy}>{t("Save all")}</Button>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      <div className="flex flex-wrap items-end gap-3">
        <label className="grid gap-1 text-xs text-zinc-600">
          {t("Room type")}
          <select className={inputCls} value={filter.room_type} onChange={(e) => setFilter({ ...filter, room_type: e.target.value })}>
            <option value="">{t("All room types")}</option>
            {roomTypes.map((o) => <option key={o.name} value={o.name}>{o.label}</option>)}
          </select>
        </label>
        <label className="grid gap-1 text-xs text-zinc-600">
          {t("From")}
          <input type="date" className={inputCls} value={filter.date_from} onChange={(e) => setFilter({ ...filter, date_from: e.target.value })} />
        </label>
        <label className="grid gap-1 text-xs text-zinc-600">
          {t("To")}
          <input type="date" className={inputCls} value={filter.date_to} onChange={(e) => setFilter({ ...filter, date_to: e.target.value })} />
        </label>
      </div>

      {rows && !rows.length && (
        <p className="rounded-lg border border-dashed border-zinc-300 px-3 py-6 text-center text-sm text-zinc-500">
          {t("No restrictions on these dates. Every day is open to sale with no minimum stay beyond the seasons'.")}
        </p>
      )}

      {rows && rows.length > 0 && (
        <Card>
          <CardContent className="divide-y divide-zinc-100 p-0">
            {rows.map((r) => (
              <div key={r.name} className={r.disabled ? "flex flex-wrap items-center gap-3 px-3 py-2.5 opacity-60" : "flex flex-wrap items-center gap-3 px-3 py-2.5"}>
                <div className="min-w-48 text-sm">
                  <div className="font-medium tabular-nums text-zinc-900">
                    {r.date_from === r.date_to ? r.date_from : `${r.date_from} → ${r.date_to}`}
                  </div>
                  <div className="text-xs text-zinc-500">
                    {r.room_type ? labelOf(r.room_type) : t("All room types")} · {r.rate_plan ? labelOf(r.rate_plan) : t("All rate plans")} · {days(r)}
                  </div>
                </div>
                <div className="flex flex-1 flex-wrap gap-1.5">
                  {tags(r).map((x) => <Badge key={x.text} tone={x.tone}>{x.text}</Badge>)}
                  {!!r.disabled && <Badge tone="zinc">{t("Off")}</Badge>}
                  {r.note && <span className="text-xs text-zinc-500">{r.note}</span>}
                </div>
                <div className="flex items-center gap-1">
                  <Button variant="ghost" aria-label={t("Edit")} onClick={() => { setNotice(null); setDraft([{ ...r }]) }} disabled={busy}>
                    <Pencil className="size-4" aria-hidden />
                  </Button>
                  <Button variant="outline" onClick={() => save([{ ...r, disabled: r.disabled ? 0 : 1 }], () => (r.disabled ? t("Restriction turned on.") : t("Restriction turned off.")))} disabled={busy}>
                    {r.disabled ? t("Turn on") : t("Turn off")}
                  </Button>
                  {deleting === r.name ? (
                    <>
                      <Button variant="outline" className="text-rose-700" onClick={() => remove(r.name!)} disabled={busy}>{t("Delete for good")}</Button>
                      <Button variant="ghost" onClick={() => setDeleting(null)}>{t("Keep")}</Button>
                    </>
                  ) : (
                    <Button variant="ghost" aria-label={t("Delete")} onClick={() => setDeleting(r.name!)} disabled={busy}>
                      <Trash2 className="size-4" aria-hidden />
                    </Button>
                  )}
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
      )}
    </div>
  )
}
