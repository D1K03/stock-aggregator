import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Palette from "@/components/Palette";
import { ScreenContextProvider } from "@/lib/screen-context";
import { StevenProvider } from "@/lib/steven";
import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  // One word, on every page. A tab strip is read at a glance and a title that
  // changes per route is harder to find again, not easier — and "morning
  // snapshot" described the overview, which is now one page of several.
  title: "Screener",
  description:
    "Multi-signal equity screener. Transparent, sector-relative pillar scores; alerts on threshold crossings.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className={`${geistSans.variable} ${geistMono.variable}`}>
        {/* The rail's width, applied before anything is painted.

            React can only read localStorage after mounting, so the first paint
            used the default and the restored width arrived one commit later —
            and because that same commit is where `ready` switches the CSS
            transition on, the correction *animated*. The rail visibly slid from
            248px to whatever it had been left at, on every refresh.

            This runs synchronously before the body renders, so the server's
            markup is already the right width and there is nothing to correct.
            Wrapped in try/catch because a browser with storage denied should
            get the default, not a blank page. */}
        <script
          dangerouslySetInnerHTML={{
            __html: `try{
  var s = localStorage.getItem('screener.sidebar.width');
  var shut = localStorage.getItem('screener.sidebar.collapsed') === '1';
  var n = Number(s);
  var w = shut ? 64 : (n >= 200 && n <= 360 ? n : 248);
  document.documentElement.style.setProperty('--rail-w', w + 'px');
  if (shut) document.documentElement.classList.add('rail-shut');
}catch(e){}`,
          }}
        />
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
