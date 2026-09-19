# CampusQuest Reference Projects and Sources

> Approved references are for extracting patterns. They are not architecture templates to copy wholesale.

References were reviewed for CampusQuest on 2026-09-19. Re-check upstream documentation when implementation happens because these projects evolve.

## 1. shadcn/ui — primary component composition reference

URL: https://ui.shadcn.com/

Useful for:

- composable accessible UI building blocks;
- dashboard, sidebar, table, and form patterns;
- CSS-variable theming;
- component ownership inside the application repository;
- project-aware Agent skill support.

CampusQuest should learn:

- composition and token customization;
- consistent primitives;
- registry-assisted implementation.

Do not copy:

- every available block;
- default styling without adapting CampusQuest tokens;
- a card-for-everything dashboard.

Official skill docs:
https://ui.shadcn.com/docs/skills

## 2. Radix Primitives — interaction and accessibility behavior reference

URL:
https://www.radix-ui.com/primitives

Useful for:

- dialogs, popovers, menus, and tabs;
- focus management;
- keyboard navigation;
- WAI-ARIA-aligned primitive behavior.

CampusQuest should learn:

- use tested primitives for complex interaction;
- preserve semantic and keyboard behavior when styling.

Do not copy:

- demo aesthetics;
- desktop-application menu patterns when ordinary website navigation is more appropriate.

Accessibility overview:
https://www.radix-ui.com/primitives/docs/overview/accessibility

## 3. FastAPI official full-stack template — end-to-end engineering reference

Repository:
https://github.com/fastapi/full-stack-fastapi-template

Useful for:

- FastAPI + PostgreSQL development discipline;
- generated frontend API clients;
- Playwright + Pytest;
- Docker Compose and local service workflow;
- authentication and recovery examples;
- Tailwind + shadcn frontend organization;
- modern Agent-skill awareness.

CampusQuest should learn:

- complete developer workflow;
- API-client generation;
- integrated test and infrastructure setup.

Do not copy:

- SQLModel persistence choice; CampusQuest plans SQLAlchemy 2.x;
- Vite/React routing architecture; CampusQuest plans Next.js;
- the template's auth and product model.

## 4. FastAPI official skill — current framework behavior reference

Source:
https://github.com/fastapi/fastapi/blob/master/fastapi/.agents/skills/fastapi/SKILL.md

Useful for:

- current FastAPI conventions;
- dependency injection;
- response models;
- streaming and SSE if needed later;
- avoiding stale framework assumptions.

Treat this as higher authority than third-party FastAPI style guides for framework API behavior.

## 5. FastAPI Best Practices by zhanymkanov — opinionated maintainability reference

Repository:
https://github.com/zhanymkanov/fastapi-best-practices

Useful for:

- domain-oriented project structure;
- async route and dependency discussion;
- Pydantic and settings separation;
- Alembic and database naming;
- async testing from day one;
- Ruff;
- anti-pattern recognition.

CampusQuest should learn:

- maintainability questions and useful engineering warnings.

Do not copy:

- a rule that contradicts our approved architecture;
- version-specific advice without checking current FastAPI and SQLAlchemy docs;
- one author's preference as a product requirement.

## 6. Vercel React Best Practices — React and Next.js performance reference

Repository:
https://github.com/vercel-labs/agent-skills

Skill:
react-best-practices

Useful for:

- eliminating async waterfalls;
- bundle control;
- server-side performance;
- client data-fetching review;
- rendering and rerender review.

CampusQuest should learn:

- prioritize high-impact performance issues;
- avoid cargo-cult memoization;
- use the skill during implementation and review.

Do not treat performance rules as permission to violate CampusQuest accessibility or product behavior.

## 7. Vercel frontend-design skill — creative UI quality reference

Source:
https://github.com/vercel-labs/open-agents/blob/main/.agents/skills/frontend-design/SKILL.md

Useful for:

- avoiding generic AI-generated UI;
- deliberate aesthetic choices;
- typography, composition, color, and motion craft.

Important CampusQuest override:

The skill encourages bold aesthetics. CampusQuest intentionally chooses **restrained academic productivity**. Use this skill to improve craft and detail, but the CampusQuest design-system document sets the aesthetic boundary.

## 8. Next.js AI-agent guide — installed-version documentation workflow

Source:
https://github.com/vercel/next.js/blob/canary/docs/01-app/02-guides/ai-agents.mdx

Useful for:

- directing agents to version-matched documentation inside the installed Next.js package;
- keeping project-specific instructions in AGENTS.md;
- reducing stale training-data assumptions.

CampusQuest rule:

After installation, read frontend/node_modules/next/dist/docs/ for the exact installed version before using uncertain Next.js APIs.

## 9. Cal.com Developer Starter Kit — token ownership and component craftsmanship reference

Repository:
https://github.com/calcom/developer-starter-kit

Useful for:

- modern Next.js App Router structure;
- Tailwind v4;
- owned component primitives;
- CSS-variable theming;
- thin API client and UI separation.

CampusQuest should learn:

- design tokens should make restyling systematic;
- application-level composition should be owned by the project.

Do not copy:

- Cal.com branding and fonts;
- scheduling-domain architecture;
- Bun or Biome choices automatically; CampusQuest tooling follows our own plans.

## 10. Dub — production Next.js product reference

Repository:
https://github.com/dubinc/dub

Useful for:

- a large real-world Next.js and TypeScript product;
- dense dashboards;
- repeated product-grade UI patterns;
- operational pages.

CampusQuest should learn:

- how mature products compose dense information and reusable primitives.

Do not copy:

- its multi-tenant and marketing stack;
- Prisma or PlanetScale choices;
- authentication and billing architecture;
- source code without considering license obligations.

Prefer conceptual inspiration or small patterns whose licensing is understood.

## 11. Ruff — Python formatter and lint reference

Docs:
https://docs.astral.sh/ruff/

CampusQuest intent:

- one Python formatting and lint toolchain;
- ruff format --check in verification;
- ruff check for lint;
- formatter-compatible lint configuration.

## 12. mypy — static type checking reference

Docs:
https://mypy.readthedocs.io/

CampusQuest intent:

- strict-first project typing;
- narrow per-library exceptions;
- no broad global disable merely to silence migration friction.

## 13. Reference selection rule

When an Agent sees multiple examples:

1. use CampusQuest product and design semantics;
2. use official framework docs for API behavior;
3. use approved references for implementation shape;
4. choose the simplest pattern preserving testability and accessibility;
5. document a deliberate deviation when a reference would otherwise tempt an architectural change.
