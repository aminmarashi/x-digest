# Persona

This file is the scoring rubric. The digest script feeds it to Claude verbatim, so write it
for the model: who I am, what I care about, what to surface, what to drop. Edit freely; the
next run picks it up.

## Who I am

Senior software engineer in Amsterdam, 10+ years across backend, full-stack, and cloud
infrastructure. AWS Certified Solutions Architect Professional. I build systems that are
useful, maintainable, and boring in the best possible way: things that work and tools that
save time.

I use AI agents as my daily development workflow, not as a curiosity. I built an orchestrator
that runs and monitors multiple Claude agents until they produce a reviewed, merge-ready pull
request (cerebro), a proxy that translates between the Anthropic and OpenAI APIs on the fly
(codex-proxy), and LLM-powered ranking for my own feed reader. I integrate Anthropic and
OpenAI models into the products I build on the side.

## What I care about (surface these)

- Practical AI engineering: coding agents, agent orchestration, multi-agent systems, context
  engineering, evals, prompt caching, tool use. Especially workflows where agents produce
  reviewed, production-ready code.
- LLM APIs and developer tooling: Claude/Anthropic, OpenAI, model releases with concrete
  capability or pricing changes, API design of AI products.
- How experienced engineers actually use AI day to day: setups, failure modes, postmortems,
  honest benchmarks.
- Serverless and AWS architecture: Lambda, API Gateway, DynamoDB, Step Functions, cost
  optimization with real numbers.
- Rust and WebAssembly, especially Rust on Lambda and WASM sandboxing.
- TypeScript/Node backend engineering, API design, distributed and message-driven systems,
  low-latency systems.
- Database internals, Postgres above all. Redis and its alternatives.
- Developer experience and testing: TDD, integration/E2E testing, CI/CD, local dev tooling.
- Feed ranking and recommender systems (I build my own feed readers).
- Engineering career topics at the senior-to-architect transition: system design, technical
  leadership without management, writing design docs that get buy-in.

## Taste

- Long-form technical content beats hot takes. A linked blog post, paper, or repo raises the
  score; prefer tweets that point somewhere with substance.
- Concrete beats abstract: numbers, benchmarks, code, architecture diagrams, postmortems.
- Contrarian is fine when argued. Skepticism about AI hype is welcome; cheerleading is not.
- Internals and "how it actually works" deep dives are always interesting.

## Drop these

- Engagement bait, threads that restate documentation, "10 tools you must know" lists.
- Vague thought leadership and motivational content.
- Crypto, token launches, growth hacking.
- Drama between accounts, screenshots of arguments.
- Product announcements with no technical detail.

## What I'm looking for right now

Growing from senior engineer toward software architecture. Building better AI-assisted
development workflows. Ideas worth stealing for my own products: a feed reader with LLM
ranking and a serverless platform built in Rust.
