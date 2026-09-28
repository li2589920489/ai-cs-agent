# 前端镜像 — Next.js 15 standalone 模式（多阶段构建）
# 说明：国内网络通过 DaoCloud 镜像源拉取 node 基础镜像，npm 走 npmmirror 加速。
# standalone 模式要求 next.config.mjs 设置 output: 'standalone'，产物在 .next/standalone。
ARG BASE_IMAGE=docker.m.daocloud.io/library/node:22-alpine
FROM ${BASE_IMAGE} AS base

# ===== 依赖阶段：只复制锁文件，最大化缓存 =====
FROM base AS deps
RUN apk add --no-cache libc6-compat
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --registry=https://registry.npmmirror.com

# ===== 构建阶段 =====
FROM base AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
# 前端鉴权凭据：.dockerignore 已排除 .env*，因此必须经 build args 注入。
# NEXT_PUBLIC_* 会被内联进客户端 bundle —— 生产环境应改为服务端 Route Handler 代理注入。
ARG NEXT_PUBLIC_TENANT_API_KEY=""
ARG NEXT_PUBLIC_AGENT_API_KEY=""
ENV NEXT_PUBLIC_TENANT_API_KEY=$NEXT_PUBLIC_TENANT_API_KEY \
    NEXT_PUBLIC_AGENT_API_KEY=$NEXT_PUBLIC_AGENT_API_KEY
RUN npm run build

# ===== 运行阶段：只带 standalone 产物，最小化镜像 =====
FROM base AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
RUN addgroup --system --gid 1001 nodejs && adduser --system --uid 1001 nextjs
COPY --from=builder /app/public ./public
RUN mkdir .next && chown nextjs:nodejs .next
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]
