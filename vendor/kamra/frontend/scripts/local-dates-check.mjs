#!/usr/bin/env node
/** Regression checks for reception dates (HR-127) and counted stay labels (HR-128), with the real translator and
 * the real dictionaries: `node scripts/local-dates-check.mjs`. */
import assert from "node:assert/strict"
import fs from "node:fs"
import path from "node:path"
import { createRequire } from "node:module"
import { fileURLToPath } from "node:url"
import ts from "typescript"

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
const require = createRequire(import.meta.url)
const dictionaries = { en: {} }
for (const language of ["es", "pt", "ar"]) {
  dictionaries[language] = JSON.parse(fs.readFileSync(path.join(root, `src/i18n/locales/${language}.json`), "utf8"))
}
let language = "en"
function load(file, imports = {}) {
  const output = ts.transpileModule(fs.readFileSync(path.join(root, file), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const api = {}
  new Function("require", "exports", output)((name) => {
    if (name in imports) return imports[name]
    throw new Error(`Unexpected import in ${file}: ${name}`)
  }, api)
  return api
}
const i18n = load("src/lib/i18n.ts", {
  react: require("react"),
  "./dir": { getLang: () => language },
  "./locales": { dictionaryFor: (key) => dictionaries[key] ?? {} },
})
const dates = load("src/lib/localDates.ts")
const { t } = i18n

const now = new Date(2026, 10, 17, 12, 0)
const synced = "2026-11-17 09:15:00"
const lastYear = "2025-12-30 23:05:59"
const ask = "The last full sync went out on {date} at {time}. The channel manager admits one a day: send another anyway?"

// Spanish and Portuguese: dd-mm (with the year only outside the current one) and hh:mm, no seconds.
assert.deepEqual(dates.momentParts(synced, "es", { now }), { date: "17-11", time: "09:15" })
assert.deepEqual(dates.momentParts(lastYear, "es", { now }), { date: "30-12-2025", time: "23:05" })
assert.deepEqual(dates.momentParts(synced, "pt", { now }), { date: "17-11", time: "09:15" })
assert.deepEqual(dates.momentParts("2026-11-17T09:15:00.123", "pt", { now }), { date: "17-11", time: "09:15" })
assert.equal(dates.momentParts("", "es", { now }), null)
assert.equal(dates.momentParts("ayer", "es", { now }), null)

language = "es"
assert.equal(t(ask, dates.momentParts(synced, language, { now })),
  "La última sincronización completa salió el 17-11 a las 09:15. El gestor de canales admite una al día: ¿desea enviar otra de todos modos?")
assert.equal(t("{date} at {time}", dates.momentParts(synced, language, { now })), "17-11 a las 09:15")
assert.equal(dates.listDate("2026-12-26", language), "26/12/2026")

language = "pt"
assert.equal(t(ask, dates.momentParts(synced, language, { now })),
  "A última sincronização completa saiu no dia 17-11 às 09:15. O gerenciador de canais aceita uma por dia: enviar outra mesmo assim?")
assert.equal(t("{date} at {time}", dates.momentParts(synced, language, { now })), "17-11 às 09:15")
assert.equal(dates.listDate("2026-12-26", language), "26/12/2026")

// English keeps the format Kamra already uses (RoomDrawer's useDates): day and short month, and hh:mm.
language = "en"
assert.deepEqual(dates.momentParts(synced, language, { locale: "en-GB", now }), { date: "17 Nov", time: "09:15" })
assert.equal(t(ask, dates.momentParts(synced, language, { locale: "en-GB", now })),
  "The last full sync went out on 17 Nov at 09:15. The channel manager admits one a day: send another anyway?")
// A moment outside the current year, and every list date, carry the year: a restriction list runs into the next one.
assert.deepEqual(dates.momentParts(lastYear, language, { locale: "en-GB", now }), { date: "30 Dec 2025", time: "23:05" })
assert.equal(dates.listDate("2026-12-26", language, "en-GB"), "26 Dec 2026")
assert.equal(dates.listDate("2027-01-04", language, "en-GB"), "4 Jan 2027")
const kamra = new Date(2026, 10, 17, 9, 15)
assert.deepEqual(dates.momentParts(synced, language, { now }), {
  date: kamra.toLocaleDateString(undefined, { day: "numeric", month: "short" }),
  time: kamra.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }),
})
assert.equal(dates.listDate("2026-12-26", language),
  new Date(2026, 11, 26).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }))
assert.equal(dates.listDate("", language), "")

// The screens show these formats, not the raw server value.
const source = (file) => fs.readFileSync(path.join(root, file), "utf8")
const actions = source("src/components/ChannelConnectionActions.tsx")
assert.match(actions, /const moment = [^]*?momentParts\(value, lang\)/, "moment() goes through momentParts")
assert.match(actions, /moment\(status\.last_full_sync\)/, "Last full sync goes through moment()")
assert.match(actions, /moment\(status\.last_push\)/, "Last push goes through moment()")
assert.match(actions, /momentParts\(confirmSince/, "The second full sync warning goes through momentParts")
assert.ok(!actions.includes("went out at {time}"), "The old warning key is gone")
const restrictions = source("src/screens/ChannelRestrictions.tsx")
assert.match(restrictions, /listDate\(r\.date_from/, "The restriction list shows date_from with listDate")
assert.match(restrictions, /listDate\(r\.date_to/, "The restriction list shows date_to with listDate")
assert.ok(!/field: "last_full_sync"/.test(source("src/screens/configs.ts")), "The raw last_full_sync field left the form")

// HR-128: a stay of one night says it in the singular, with the plural of the i18n (tn), in every language.
const { tn } = i18n
const maxStay = (n) => tn(n, "Max. stay: {n} night", "Max. stay: {n} nights")
for (const [lang, one, two] of [
  ["es", "Estadía máx.: 1 noche", "Estadía máx.: 2 noches"],
  ["pt", "Estadia máx.: 1 noite", "Estadia máx.: 2 noites"],
  ["en", "Max. stay: 1 night", "Max. stay: 2 nights"],
]) {
  language = lang
  assert.equal(maxStay(1), one, `${lang}: a one-night maximum stay is singular`)
  assert.equal(maxStay(2), two, `${lang}: two nights or more stay plural`)
}
assert.match(restrictions, /tn\(r\.max_stay, "Max\. stay: \{n\} night", "Max\. stay: \{n\} nights"/,
  "The restriction list counts the maximum stay with tn")

console.log("local dates and stay checks passed: es, pt and en")
