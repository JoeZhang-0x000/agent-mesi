import { tool } from "@opencode-ai/plugin"

const PYTHON = process.env.MESI_PYTHON ?? "/Users/zhangxin/Desktop/Aeloon-Pro/.venv/bin/python"

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
  return await Bun.$`env PYTHONPATH=${`${root}/src:${root}`} MESI_PROJECT_ROOT=${root} MESI_AGENT_ID=${agent} ${PYTHON} -m mesi_runtime ${args}`.cwd(context.directory).text()
}

export default tool({
  description: "Write a file through MESI coherence checks",
  args: {
    path: tool.schema.string().describe("Project-relative path to write"),
    content: tool.schema.string().describe("Full file content"),
  },
  async execute(args, context) {
    return (await runMesi(context, ["tool", "write", args.path, args.content])).trim()
  },
})
