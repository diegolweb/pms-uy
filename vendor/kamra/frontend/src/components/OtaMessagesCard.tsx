import { useEffect, useState } from "react"
import { Link } from "react-router-dom"
import { call, getCurrentProperty } from "../lib/api"
import { useAuth } from "../lib/auth"
import { cn } from "../lib/utils"
import { t as translate, uiDateLocale, useUILanguage } from "../lib/i18n"

/* The OTA guest messages (Booking.com, Expedia, Airbnb through Channex):
   the box on a reservation, and the helpers the inbox screen shares. */

export interface OtaThread {
  thread: string
  provider: string
  platform: string
  reservation: string | null
  guest_name: string
  check_in: string | null
  check_out: string | null
  nights: number | null
  room_type: string | null
  last_at: string | null
  last_sender: "guest" | "property" | "system" | null
  unanswered: boolean
  no_reply_needed: boolean
  is_closed: boolean
  preview: string
}

const parse = (d: string) => new Date(d.replace(" ", "T"))
const dayStart = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()

/** "hace 20 min", "ayer 22:40", "lun 10:15", "12 nov": a message's time. */
export function whenLabel(d: string | null): string {
  if (!d) return ""
  const at = parse(d)
  if (Number.isNaN(at.getTime())) return ""
  const now = new Date()
  const lang = uiDateLocale(undefined)
  const rtf = new Intl.RelativeTimeFormat(lang, { numeric: "auto", style: "short" })
  const secs = (now.getTime() - at.getTime()) / 1000
  if (secs < 60) return rtf.format(0, "second")
  if (secs < 3600) return rtf.format(-Math.round(secs / 60), "minute")
  const time = at.toLocaleTimeString(lang, { hour: "2-digit", minute: "2-digit" })
  const days = Math.round((dayStart(now) - dayStart(at)) / 86_400_000)
  if (days <= 0) return secs < 6 * 3600 ? rtf.format(-Math.round(secs / 3600), "hour") : time
  if (days === 1) return `${rtf.format(-1, "day")} ${time}`
  if (days < 7) return `${at.toLocaleDateString(lang, { weekday: "short" })} ${time}`
  return at.toLocaleDateString(lang, { day: "numeric", month: "short" })
}

/** "12 dic" from 2026-12-12. */
export function shortDate(d: string): string {
  const [y, m, day] = d.split("-").map(Number)
  return new Date(y, m - 1, day).toLocaleDateString(uiDateLocale(undefined), {
    day: "numeric",
    month: "short",
  })
}

/** "12 al 15 dic" (the day alone when both fall in the same month). */
export function stayRange(start: string, end: string): string {
  const sameMonth = start.slice(0, 7) === end.slice(0, 7)
  return translate("{start} to {end}", {
    start: sameMonth ? String(Number(start.slice(8, 10))) : shortDate(start),
    end: shortDate(end),
  })
}

export function PlatformPill({ provider, platform }: { provider: string; platform: string }) {
  return (
    <span
      className={cn(
        "mr-1 inline-block rounded-md px-1.5 py-px text-[10px] font-semibold",
        provider === "BookingCom" && "bg-blue-50 text-blue-700",
        provider === "Expedia" && "bg-amber-50 text-amber-800",
        provider !== "BookingCom" && provider !== "Expedia" && "bg-rose-50 text-rose-700",
      )}
    >
      {platform}
    </span>
  )
}

export function UnansweredDot() {
  return <span className="mr-1.5 inline-block size-[7px] shrink-0 rounded-full bg-red-600" aria-hidden />
}

interface ReservationThread {
  thread: string
  provider: string
  platform: string
  unanswered: boolean
  last_at: string | null
}

/** On a reservation: its OTA conversations, if it has any (nothing otherwise,
    and nothing for whoever may not answer them). */
export default function OtaMessagesCard({ reservation }: { reservation: string }) {
  useUILanguage()
  const property = getCurrentProperty()
  const { allowed } = useAuth()
  const mayAnswer = allowed("Channel Message Thread", "reply_messages")
  const [rows, setRows] = useState<ReservationThread[]>([])

  useEffect(() => {
    if (!mayAnswer) return
    let live = true
    call<ReservationThread[]>("kamra.channel_messages.for_reservation", { property, reservation })
      .then((r) => live && setRows(r ?? []))
      .catch(() => live && setRows([]))
    return () => {
      live = false
    }
  }, [property, reservation, mayAnswer])

  if (!mayAnswer || !rows.length) return null
  return (
    <div className="space-y-4 rounded-xl border border-zinc-200 bg-white p-4" data-testid="ota-messages">
      {rows.map((r) => (
        <div key={r.thread}>
          <h3 className="text-sm font-semibold text-zinc-800">
            {translate("Messages from {platform}", { platform: r.platform })}
          </h3>
          <p className="mt-1 flex items-center text-sm text-zinc-600">
            {r.unanswered && <UnansweredDot />}
            {r.unanswered
              ? translate("Unanswered · the latest, {ago}", { ago: whenLabel(r.last_at) })
              : translate("Answered · the latest, {ago}", { ago: whenLabel(r.last_at) })}
          </p>
          <Link
            to={`/ota-messages?thread=${encodeURIComponent(r.thread)}`}
            className="mt-3 inline-flex rounded-lg bg-brand-600 px-3.5 py-2 text-sm font-semibold text-white hover:bg-brand-700"
          >
            {translate("Open the conversation")}
          </Link>
        </div>
      ))}
    </div>
  )
}
