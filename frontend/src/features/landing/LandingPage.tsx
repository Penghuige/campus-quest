/** The approved document owns its WebGL/history/CSS lifecycle inside one same-origin frame. */
export function LandingPage() {
  const basePath = process.env.NEXT_PUBLIC_APP_BASE_PATH ?? "";
  return (
    <main className="landing-page">
      <iframe className="landing-frame" title="智汇华师校园介绍" src={`${basePath}/landing/index.html`} />
      <noscript><p>请启用 JavaScript 体验校园介绍，或<a className="link" href={`${basePath}/login`}>直接登录智汇华师</a>。</p></noscript>
    </main>
  );
}
