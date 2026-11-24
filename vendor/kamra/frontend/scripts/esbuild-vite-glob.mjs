/** esbuild plugin for the desk's browser tests: Vite's `import.meta.glob` (Kamra 2.8 loads its locale
 *  dictionaries with it in src/lib/locales.ts) does not exist in esbuild, and the bundle stopped at
 *  "(intermediate value).glob is not a function" before the first paint. Each
 *  `import.meta.glob("<dir>/*.<ext>")` becomes the object Vite would build: { "<dir>/<file>": () => import(...) }. */
import { readdirSync } from "node:fs"
import { readFile } from "node:fs/promises"
import { dirname, posix, resolve } from "node:path"

const GLOB = /import\.meta\.glob(?:<[^>]*>)?\(\s*"([^"*]+)\/\*(\.[A-Za-z0-9]+)"\s*\)/g

export const viteGlob = {
  name: "vite-import-meta-glob",
  setup(build) {
    build.onLoad({ filter: /[\\/]src[\\/].*\.tsx?$/ }, async (args) => {
      const code = await readFile(args.path, "utf8")
      if (!code.includes("import.meta.glob")) return undefined
      const contents = code.replace(GLOB, (_, dir, ext) => {
        const files = readdirSync(resolve(dirname(args.path), dir)).filter((f) => f.endsWith(ext)).sort()
        const entries = files.map((f) => {
          const rel = posix.join(dir, f).replace(/^(?!\.)/, "./")
          return `${JSON.stringify(rel)}: () => import(${JSON.stringify(rel)})`
        })
        return `({${entries.join(", ")}})`
      })
      return { contents, loader: args.path.endsWith(".tsx") ? "tsx" : "ts" }
    })
  },
}
