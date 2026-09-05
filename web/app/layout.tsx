import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Palette from "@/components/Palette";
import { ScreenContextProvider } from "@/lib/screen-context";
import { StevenProvider } from "@/lib/steven";
import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Screener — morning snapshot",
  description:
    "Multi-signal equity screener. Transparent, sector-relative pillar scores; alerts on threshold crossings.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className={`${geistSans.variable} ${geistMono.variable}`}>
        <ScreenContextProvider>
          {/* Above both surfaces that talk to Steven, so the palette and the
              Steven page are two views of one conversation rather than two
              conversations that happen to look alike. */}
          <StevenProvider>
            {children}
            <Palette />
          </StevenProvider>
        </ScreenContextProvider>
      </body>
    </html>
  );
}
