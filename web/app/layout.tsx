import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "API Integration Engine",
  description:
    "Natural-language automation goals become validated, executable API workflows.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header className="top">
          <div className="shell">
            <span className="brand">API Integration Engine</span>
            <nav>
              <Link href="/">Search</Link>
              <Link href="/create">Create</Link>
              <Link href="/credentials">Credentials</Link>
              <Link href="/executions">Runs</Link>
            </nav>
          </div>
        </header>
        <main className="shell">{children}</main>
      </body>
    </html>
  );
}
