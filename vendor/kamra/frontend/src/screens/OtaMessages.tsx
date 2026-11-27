import { useCallback, useEffect, useRef, useState } from "react"
import { MessagesSquare, Send } from "lucide-react"
import { Link, useSearchParams } from "react-router-dom"
import { call, getCurrentProperty } from "../lib/api"
import { serverError } from "../lib/resource"
import { Button } from "../components/ui/button"
import { cn } from "../lib/utils"
import { useT, t as translate, tn } from "../lib/i18n"
import {
  PlatformPill,
  UnansweredDot,
  shortDate,
  stayRange,
  whenLabel,
  type OtaThread,
} from "../components/OtaMessagesCard"

/* The desk's inbox for the guest messages of Booking.com, Expedia and Airbnb
   (through Channex): conversations on the left, the open one on the right,
   and the reply goes to the guest on their platform. Same layout as the
   WhatsApp inbox. Kamra keeps no copy of the messages: they are read live. */

interface Msg {
  id: string
  sender: "guest" | "property" | "system"
  text: string
  at: string | null
  attachments: { index: number }[]
}
interface ThreadsReply {
  app_missing: boolean
  connected: boolean
  is_admin: boolean
  threads: OtaThread[]
}

export default function OtaMessages() {
  const { t } = useT()
  const property = getCurrentProperty()
  const [params, setParams] = useSearchParams()
  const [data, setData] = useState<ThreadsReply | null>(null)
  const [filter, setFilter] = useState<"unanswered" | "all">("unanswered")
  const [active, setActive] = useState<string | null>(params.get("thread"))
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [draft, setDraft] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  const threads = data?.threads ?? []
  const unanswered = threads.filter((th) => th.unanswered)
  const shown = filter === "unanswered" ? unanswered : threads
  const activeThread = threads.find((th) => th.thread === active)

  const loadThreads = useCallback(() => {
    call<ThreadsReply>("kamra.channel_messages.threads", { property })
      .then(setData)
      .catch((e) => setError(serverError(e)))
  }, [property])

  useEffect(() => {
    loadThreads()
    const id = setInterval(loadThreads, 30_000)
    return () => clearInterval(id)
  }, [loadThreads])

  // the thread of the link (?thread=, from the bell or a reservation) shows
  // even when it is already answered; with none, the first one opens
  useEffect(() => {
    if (!data) return
    if (active && !unanswered.some((th) => th.thread === active) && threads.some((th) => th.thread === active)) {
      setFilter("all")
    } else if (!active && shown[0]) {
      setActive(shown[0].thread)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data])

  const activeLastAt = activeThread?.last_at
  const loadThread = useCallback(() => {
    if (!active) return setMsgs([])
    call<{ messages: Msg[] }>("kamra.channel_messages.thread", { property, thread: active })
      .then((d) => setMsgs(d.messages))
      .catch((e) => setError(serverError(e)))
  }, [property, active])

  useEffect(() => {
    loadThread()
  }, [loadThread, activeLastAt])
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [msgs.length])

  function open(thread: string) {
    setActive(thread)
    setDraft("")
    setError(null)
    setParams({ thread }, { replace: true })
  }

  async function sendReply() {
    if (!draft.trim() || !active) return
    setBusy(true)
    setError(null)
    try {
      const sent = await call<Msg>("kamra.channel_messages.reply", { property, thread: active, text: draft.trim() })
      setDraft("")
      setMsgs((m) => [...m, sent])
      loadThreads()
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  async function markNoReply() {
    if (!active) return
    setBusy(true)
    setError(null)
    try {
      await call("kamra.channel_messages.no_reply_needed", { property, thread: active })
      loadThreads()
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  const attachmentHref = (m: Msg, index: number) =>
    `/api/method/kamra.channel_messages.attachment?${new URLSearchParams({
      property: property ?? "",
      thread: active ?? "",
      message: m.id,
      index: String(index),
    })}`

  const empty = (text: string) => (
    <div className="rounded-2xl border border-dashed border-zinc-300 p-10 text-center">
      <MessagesSquare className="mx-auto size-8 text-zinc-300" aria-hidden />
      <p className="mt-3 text-sm text-zinc-600">{text}</p>
    </div>
  )

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">{t("OTA Messages")}</h1>
        <p className="mt-1 text-sm text-zinc-500">
          {t("What guests write on Booking.com and Expedia. What you answer here reaches the guest on their platform.")}
        </p>
      </header>

      {error && <p className="text-sm text-rose-600">{translate(error)}</p>}

      {data?.app_missing ? (
        data.is_admin ? (
          <div className="rounded-2xl border border-amber-200 bg-amber-50 p-5 text-sm text-amber-900">
            {t("Platform messages are not enabled in Channex.")}
          </div>
        ) : (
          empty(t("No messages from the platforms yet."))
        )
      ) : data && threads.length === 0 ? (
        empty(t("No messages from the platforms yet."))
      ) : (
        <>
          <div className="flex gap-2">
            {(["unanswered", "all"] as const).map((f) => (
              <button
                key={f}
                onClick={() => setFilter(f)}
                className={cn(
                  "rounded-full border px-3 py-1 text-sm transition",
                  filter === f
                    ? "border-brand-600 bg-brand-600 font-semibold text-white"
                    : "border-zinc-200 bg-white text-zinc-600 hover:bg-zinc-50",
                )}
              >
                {f === "unanswered" ? t("Unanswered ({n})", { n: unanswered.length }) : t("All conversations")}
              </button>
            ))}
          </div>

          <div className="grid gap-4 lg:grid-cols-3">
            <div className="rounded-2xl border border-zinc-200 bg-white">
              {data && shown.length === 0 ? (
                <p className="p-6 text-center text-sm text-zinc-500">{t("No unanswered messages.")}</p>
              ) : (
                <ul className="max-h-[70vh] divide-y divide-zinc-100 overflow-y-auto">
                  {shown.map((th) => (
                    <li key={th.thread}>
                      <button
                        onClick={() => open(th.thread)}
                        className={cn(
                          "w-full px-4 py-3 text-left transition hover:bg-zinc-50",
                          active === th.thread && "bg-brand-50",
                        )}
                      >
                        <div className="flex items-baseline justify-between gap-2">
                          <span className="flex min-w-0 items-center text-sm font-semibold text-zinc-800">
                            {th.unanswered && <UnansweredDot />}
                            <span className="truncate">{th.guest_name}</span>
                          </span>
                          <span className="shrink-0 text-[11px] text-zinc-400">{whenLabel(th.last_at)}</span>
                        </div>
                        <p className="mt-0.5 text-[11px] text-zinc-500">
                          <PlatformPill provider={th.provider} platform={th.platform} />
                          {th.check_in && t("Arrives {date}", { date: shortDate(th.check_in) })}
                          {th.check_in && th.nights ? ` · ${tn(th.nights, "{n} night", "{n} nights")}` : ""}
                        </p>
                        {th.preview && (
                          <p className="mt-0.5 truncate text-xs text-zinc-600">
                            {th.last_sender === "property" ? t("You: {text}", { text: th.preview }) : th.preview}
                          </p>
                        )}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <div className="flex max-h-[70vh] min-h-[24rem] flex-col rounded-2xl border border-zinc-200 bg-white lg:col-span-2">
              {activeThread && (
                <div className="flex items-center gap-3 border-b border-zinc-100 px-4 py-3">
                  <div className="min-w-0">
                    <p className="truncate font-semibold text-zinc-800">{activeThread.guest_name}</p>
                    <p className="text-xs text-zinc-500">
                      <PlatformPill provider={activeThread.provider} platform={activeThread.platform} />
                      {[
                        activeThread.room_type,
                        activeThread.check_in && activeThread.check_out
                          ? stayRange(activeThread.check_in, activeThread.check_out)
                          : null,
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    </p>
                  </div>
                  {activeThread.reservation && (
                    <Link
                      to={`/reservations?open=${encodeURIComponent(activeThread.reservation)}`}
                      className="ml-auto shrink-0 text-xs font-semibold text-brand-700 hover:underline"
                    >
                      {t("Open the booking →")}
                    </Link>
                  )}
                </div>
              )}
              <div className="flex-1 space-y-2 overflow-y-auto bg-zinc-50/60 p-4">
                {msgs.map((m) => (
                  <div
                    key={m.id}
                    className={cn(
                      "max-w-[75%] rounded-2xl px-3.5 py-2 text-sm",
                      m.sender === "property" && "ml-auto rounded-br-md bg-brand-600 text-white",
                      m.sender === "guest" && "rounded-bl-md border border-zinc-200 bg-white text-zinc-800",
                      m.sender === "system" && "mx-auto bg-zinc-100 text-center text-xs text-zinc-500",
                    )}
                  >
                    {m.text && <p className="whitespace-pre-wrap">{m.text}</p>}
                    {m.attachments.map((a) => (
                      <a
                        key={a.index}
                        href={attachmentHref(m, a.index)}
                        target="_blank"
                        rel="noreferrer"
                        className={cn(
                          "mt-1 block text-xs font-semibold hover:underline",
                          m.sender === "property" ? "text-white" : "text-brand-700",
                        )}
                      >
                        {t("View attachment")}
                      </a>
                    ))}
                    <p
                      className={cn(
                        "mt-1 text-[10px]",
                        m.sender === "property" ? "text-brand-100" : "text-zinc-400",
                      )}
                    >
                      {m.sender === "property" ? `${t("Hotel")} · ${whenLabel(m.at)}` : whenLabel(m.at)}
                    </p>
                  </div>
                ))}
                <div ref={bottomRef} />
              </div>
              {activeThread && (
                <div className="border-t border-zinc-100 p-3">
                  <textarea
                    className="h-14 w-full resize-none rounded-xl border border-zinc-300 bg-white px-3 py-2 text-sm focus:outline-2 focus:outline-offset-1 focus:outline-brand-600"
                    placeholder={t("Write your reply…")}
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && (e.ctrlKey || e.metaKey) && sendReply()}
                  />
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <Button disabled={busy || !draft.trim()} onClick={sendReply}>
                      <Send className="size-4" aria-hidden />
                      {t("Send")}
                    </Button>
                    {activeThread.provider === "BookingCom" && (
                      <Button variant="outline" disabled={busy || !activeThread.unanswered} onClick={markNoReply}>
                        {t("No reply needed")}
                      </Button>
                    )}
                    <span className="ml-auto text-[11px] text-zinc-500">
                      {t("Reaches the guest on {platform}", { platform: activeThread.platform })}
                    </span>
                  </div>
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  )
}
