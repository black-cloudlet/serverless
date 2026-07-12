import type { Metadata } from "next";

import { branding } from "@/lib/config";

import "./globals.css";

export const metadata: Metadata = {
  title: branding.productName,
  description: `${branding.productName} - self-service console for the ${branding.organization} platform.`,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
