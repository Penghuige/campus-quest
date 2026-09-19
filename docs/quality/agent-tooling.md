# CampusQuest Agent Tooling and Skills

> Skills and tools improve implementation quality. They do not override the CampusQuest specification, implementation plans, or AGENTS.md.

## 1. Default workflow skills

When available in the runtime, use the Superpowers workflow skill appropriate to the stage:

- brainstorming — before designing new behavior;
- writing-plans — after approved architectural specs;
- test-driven-development — before implementing a feature or bug fix;
- systematic-debugging — before proposing fixes to unexplained failures;
- requesting-code-review — after completing a meaningful implementation task;
- receiving-code-review — before acting on review feedback;
- verification-before-completion — before claiming success;
- dispatching-parallel-agents or subagent-driven-development — when executing independent plan tasks safely.

Do not create project code before the relevant design or plan gate has been satisfied.

## 2. shadcn/ui skill

Official docs:
https://ui.shadcn.com/docs/skills

Purpose:

- project-aware shadcn component usage;
- correct CLI and component patterns;
- theming and registry awareness.

Current official docs show installation through the Skills CLI, for example:

~~~bash
pnpm dlx skills add shadcn/ui
~~~

Install only after the frontend has been scaffolded and the shadcn setup has reached the point where project configuration can be inspected.

After installation, verify the skill reads the actual CampusQuest configuration before relying on it.

Use for:

- adding or adapting shadcn components;
- discovering correct composition;
- theme and token work.

Do not use the skill as a design director. CampusQuest's frontend-design-system.md remains authoritative.

## 3. FastAPI official skill

Official source:
https://github.com/fastapi/fastapi/blob/master/fastapi/.agents/skills/fastapi/SKILL.md

The modern FastAPI package and repository expose an official FastAPI skill.

When the Python environment is installed, prefer package-provided or library-skill discovery where available instead of copying a stale skill file into the repository manually.

The official FastAPI full-stack template demonstrates an .agents/skills and library-skills workflow.

Use for:

- FastAPI APIs;
- dependencies;
- Pydantic request and response handling;
- current framework conventions;
- streaming and SSE if later needed.

CampusQuest-specific service, repository, transaction, privacy, and concurrency rules still come from backend-engineering.md.

## 4. Vercel React best-practices skill

Repository:
https://github.com/vercel-labs/agent-skills

Skill:
react-best-practices

Use when:

- writing or reviewing Next.js and React code;
- designing data-fetching architecture;
- reviewing bundle and rendering performance;
- inspecting server and client boundaries.

Prioritize its high-impact categories such as waterfalls and bundle or server performance before low-impact micro-optimization.

Before installation, use the repository's current Skills CLI instructions because commands and skill names may evolve.

## 5. Web design guidelines skill

Vercel's agent-skills repository also exposes a web-design-guidelines skill aimed at UI, accessibility, and UX review.

Use it as a **review skill**, not as the source of CampusQuest visual identity.

Recommended flow:

1. implement against CampusQuest design docs;
2. run the design and accessibility review skill;
3. resolve findings without changing product semantics.

Because upstream review guidance can evolve, pin or record the version when reproducibility matters.

## 6. frontend-design skill

Source:
https://github.com/vercel-labs/open-agents/blob/main/.agents/skills/frontend-design/SKILL.md

Use selectively when creating a new high-level page composition or visual treatment.

Important override:

CampusQuest does not want every page to choose a new bold aesthetic. The project has one coherent visual direction. Use this skill for craft and detail, not to reinvent the design language per task.

## 7. Next.js local docs

This is more important than a generic Next.js skill.

After next is installed, read:

~~~text
frontend/node_modules/next/dist/docs/
~~~

Use documentation matching the installed Next.js version.

Do this before uncertain work involving:

- App Router;
- caching;
- Server Components;
- Server Actions;
- middleware or proxy behavior;
- metadata;
- route handlers;
- bundling and runtime behavior.

Do not assume an API works because it existed in a previous major Next.js version.

## 8. Figma workflow

Figma is optional and valuable for visually sensitive work.

Recommended use:

- establish core frames;
- compare layout options;
- inspect spacing and token consistency;
- document component variants;
- hand a visual reference to the implementation Agent.

Required principle:

**code tokens are runtime truth.**

Do not maintain an untracked second token system in Figma.

Suggested Figma structure:

~~~text
CampusQuest
├── Foundations
│   ├── Color roles
│   ├── Typography
│   ├── Spacing
│   └── Radius and elevation
├── Components
│   ├── Buttons
│   ├── Status and Rarity
│   ├── Cards
│   ├── Tables
│   ├── Forms
│   └── Navigation
└── Product Frames
    ├── Student
    ├── Teacher
    └── Admin
~~~

If a Figma-to-code workflow is used, generated code still passes the normal quality gates.

## 9. Package-provided skill discovery

Modern libraries increasingly ship .agents/skills.

The FastAPI full-stack template includes a library-skills tool for discovering package-provided skills after dependencies are installed.

When the project is scaffolded, an Agent may evaluate:

~~~bash
uvx library-skills scan --json
# or
npx library-skills scan --json
~~~

before manually copying skill files.

Do not install every discovered skill automatically. Choose only skills relevant to CampusQuest and record why.

## 10. Skill installation policy

Before adding a skill:

1. verify the source is trusted;
2. read the skill file;
3. check license;
4. check whether it runs scripts or fetches remote instructions;
5. prefer official framework or vendor skills;
6. pin or record version when reproducibility matters;
7. verify it does not conflict with CampusQuest instructions.

Do not blindly run an all-skills installation command for third-party collections.

## 11. Review recipe by task type

### New frontend page

Use:

- CampusQuest frontend design system;
- frontend patterns;
- installed Next.js docs;
- shadcn skill if component work is involved;
- React best-practices skill;
- web-design-guidelines review.

### Backend feature

Use:

- CampusQuest backend engineering standard;
- FastAPI official skill and docs;
- TDD;
- PostgreSQL integration tests where invariants or locking are involved;
- code review and verification skills.

### UI polish task

Use:

- frontend-design-system;
- optional Figma;
- optional frontend-design skill;
- web design and accessibility review;
- screenshot or browser verification.

### Bug

Use:

- systematic debugging;
- failing regression test;
- relevant project quality document;
- verification-before-completion.

## 12. Do not create a CampusQuest custom skill yet

Project-specific mechanical rules belong in AGENTS.md, quality documents, linters, tests, and CI.

Create a custom CampusQuest skill later only if:

- the workflow is conditional or non-obvious;
- multiple Agents repeatedly fail in the same judgment-heavy way;
- static checks cannot enforce it;
- a baseline-versus-skill test shows the skill improves behavior.

Until then, prefer explicit repository instructions and automated gates.
