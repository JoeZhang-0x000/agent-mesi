import { tool } from "@opencode-ai/plugin"

const PYTHON = process.env.MESI_PYTHON ?? "/Users/zhangxin/Desktop/Aeloon-Pro/.venv/bin/python"
const MESI_RUNTIME_PYTHONPATH = "/Users/zhangxin/Desktop/multi-agents/src"

function projectRoot(directory: string, worktree?: string) {
  const marker = "/.mesi/ws/"
  const idx = directory.indexOf(marker)
  if (idx >= 0) return directory.slice(0, idx)
  return worktree || directory
}

function agentId(directory: string) {
  const parts = directory.split("/")
  const idx = parts.indexOf(".mesi")
  if (idx >= 0 && parts[idx + 1] === "ws" && parts[idx + 2]) return parts[idx + 2]
  return process.env.MESI_AGENT_ID || "default"
}

async function runMesi(context: any, args: string[]) {
  const root = projectRoot(context.directory, context.worktree)
  const agent = agentId(context.directory)
  const pythonPath = [MESI_RUNTIME_PYTHONPATH, `${root}/src`, root, process.env.PYTHONPATH ?? ""].filter(Boolean).join(":")
  const result = await Bun.$`env PYTHONPATH=${pythonPath} MESI_PROJECT_ROOT=${root} MESI_AGENT_ID=${agent} ${PYTHON} -m mesi_runtime ${args}`.cwd(context.directory).quiet().nothrow()
  const output = `${result.stdout.toString()}${result.stderr.toString()}`
  if (output.trim()) return output
  if (result.exitCode !== 0) return `MESI tool failed with exit code ${result.exitCode}`
  return ""
}

export default tool({
  description: "Apply a patch through MESI observed-write tracking",
  args: {
    patchText: tool.schema.string().describe("Unified diff or Begin Patch text"),
  },
  async execute(args, context) {
    return (await runMesi(context, ["tool", "apply-patch", args.patchText])).trim()
  },
})
