import './globals.css';
export const metadata = {
  title: 'Career Desk · 나의 다음 커리어',
  description: '새로운 기회부터 지원 결과까지, 나만의 이직 대시보드',
  robots: { index: false, follow: false },
  icons: { icon: '/favicon.svg' },
};
export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="ko">
      <body>{children}</body>
    </html>
  );
}
