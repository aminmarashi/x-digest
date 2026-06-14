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

- Long-form technical content beats hot takes. A linked blog post, paper, or repo is a plus
  only when the tweet itself states the concrete substance. A bare "here's a cool post"
  announcement that just points at the content is not, and should never be auto-reposted or
  liked, however good the linked thing is.
- Concrete beats abstract: numbers, benchmarks, code, architecture diagrams, postmortems.
- Contrarian is fine when argued. Skepticism about AI hype is welcome; cheerleading is not.
- Internals and "how it actually works" deep dives are always interesting.

## Technical only (hard filter)

I want technical substance: engineering, systems, code, architecture, research results and
methods, benchmarks, how things actually work, tools and APIs. If a post is not technical, I
do not want it, even when the topic is one I otherwise care about. In particular, filter out
(regardless of topical match):

- Complaints or commentary about a company's or product's policy, pricing, or behavior.
- Fear, doom, or hype about the future of AI; "AI will/won't take our jobs" takes.
- Opinion, punditry, predictions, and hot takes with no technical content to learn from.
- Career, hiring, funding, and business gossip; industry drama.
- AI wow-demos and capability flexes: "look what it built in N minutes" with no method,
  numbers, or how-it-works. The impressive output is not the substance; the method is.
- Jokes, satire, memes, and bits, even in fluent technical language. Judge tone and intent,
  not vocabulary -- a sentence full of real terms can still be a gag.
- Bare link-drops, announcements, and amplification where the substance lives entirely in
  the link and the tweet itself says nothing concrete.

The test: would I learn something I could act on or build with? If it's just someone's view,
drop it.

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
