/** @type {import('next').NextConfig} */
// 后端地址：本地开发默认 8000；需要指向其他端口时用 BACKEND_URL 覆盖
// （生产环境由 nginx 反代 /api/，不会走到这里的 rewrites）
const BACKEND_URL = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

const nextConfig = {
  output: 'standalone',
  devIndicators: false,
  // Proxy /chat requests to the backend server
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${BACKEND_URL}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
