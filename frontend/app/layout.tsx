import type { Metadata, Viewport } from "next";
import { Heebo } from "next/font/google";
import AppShell from "@/components/AppShell";
import "./globals.css";

const heebo = Heebo({
  subsets: ["hebrew", "latin"],
  variable: "--font-heebo",
  display: "swap",
});

export const metadata: Metadata = {
  title: "מאגר הידע של המשרד",
  description: "חיפוש ושאלות על דוחות השמאות של המשרד",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="he" dir="rtl" className={heebo.variable}>
      <body className={heebo.className}>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
