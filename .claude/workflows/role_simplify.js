export const meta = {
  name: 'role_simplify',
  description: 'Simplify-review one tier of roles ranked by mise run roles:complexity; stops at the numbered findings list',
  whenToUse:
    'Hunt for simplification across the most complex Ansible roles. Feeds apply_findings once the operator picks numbers.',
  phases: [{ title: 'Rank', detail: 'mise run roles:complexity --json' }],
}

// args: {tier?: 1, tier_size?: 10, roles?: [names], lenses?: ["simplify"], repo?}
// Tier k is ranks (k-1)*tier_size+1 .. k*tier_size; `roles` overrides it.
// Review and consolidation run in the user-level patch_review workflow, so
// findings land in <git-common-dir>/patch_review/roles_<label>/ for apply_findings.
const opts = args ?? {}
const tier = opts.tier ?? 1
const size = opts.tier_size ?? 10
const repo = opts.repo ?? 'the homelab repository (the current one)'

const RANKING = {
  type: 'object',
  properties: {
    roles: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          rank: { type: 'integer' },
          role: { type: 'string' },
          score: { type: 'number' },
          lines: { type: 'integer' },
          tasks: { type: 'integer' },
          whens: { type: 'integer' },
          cells: { type: 'integer' },
        },
        required: ['rank', 'role', 'score', 'lines', 'tasks', 'whens', 'cells'],
      },
    },
  },
  required: ['roles'],
}

phase('Rank')
const ranking = await agent(
  `In ${repo}, run \`mise run --quiet roles:complexity --json\` and return its array unchanged as roles.`,
  { label: 'rank', schema: RANKING },
)
if (!ranking?.roles?.length) throw new Error('roles:complexity returned no roles')

const picked = opts.roles
  ? ranking.roles.filter((r) => opts.roles.includes(r.role))
  : ranking.roles.filter((r) => r.rank > (tier - 1) * size && r.rank <= tier * size)
if (!picked.length) throw new Error(`no roles in tier ${tier} (size ${size}) or matching ${opts.roles}`)
log(`roles: ${picked.map((r) => `${r.rank}. ${r.role} (${r.score})`).join(', ')}`)

const targets = picked.map((r) => ({
  id: r.role,
  title: `role ${r.role} (rank ${r.rank}: ${r.lines} lines, ${r.tasks} tasks, ${r.whens} when, ${r.cells} test cells)`,
  files: [
    `roles/${r.role}/ (the whole role)`,
    `plus whatever exercises it: its meta/test.yml cells, test/ fixtures, unit_tests/ that load its files, and the playbooks or group_vars that set its variables`,
  ],
}))

return await workflow('patch_review', {
  targets,
  repo: opts.repo,
  lenses: opts.lenses ?? ['simplify'],
  out: opts.roles ? `roles_${opts.roles.join('_')}` : `roles_tier${tier}`,
})
