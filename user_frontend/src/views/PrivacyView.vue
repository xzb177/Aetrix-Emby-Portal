<template>
  <main class="privacy-page">
    <div class="privacy-inner">
      <p class="au-eyebrow privacy-eyebrow">Aetrix</p>
      <h1 class="privacy-title">隐私政策</h1>
      <p class="privacy-note">
        以下内容适用于你通过 Aetrix 绑定自己的谷歌盘的情形。
      </p>
      <ol class="privacy-list">
        <li>
          <strong>我们访问什么：</strong>只有在你主动在账号设置里绑定自己的谷歌盘之后，
          Aetrix 才会通过 Google OAuth 访问你的 Google Drive。drive.readonly 只读读取
          你盘里媒体文件的名称、元数据和文件内容；drive.file 的写入只限在你自己绑定的盘里
          建转存目录、存转存副本，绝不删除、分享、改动你盘里的其他文件，也绝不碰别的用户的盘。
        </li>
        <li>
          <strong>用来干什么：</strong>只用来做三件事——（1）把你自己盘里的影片建成你的媒体库；
          （2）你点播时用你自己的账号额度取流播放；（3）把你还没转存过的片子先转存到你自己的盘里
          （只在你绑定的盘里建转存目录、存转存副本），转完再播。你的盘只服务你自己，
          不会给别的用户播，也不会进共享池。
        </li>
        <li>
          <strong>凭据怎么存：</strong>Google 发来的 access_token / refresh_token 在服务器端
          密封保存，不在页面上显示，不写进日志，不交给任何第三方。
        </li>
        <li>
          <strong>数据去向：</strong>我们不卖你的数据，不用你的文件做广告，不用你的文件训练模型，
          不把你的 Drive 数据共享给任何第三方。
        </li>
        <li>
          <strong>你随时能停：</strong>在账号设置里解绑，对应凭据即删除；你也可以随时在
          Google 账号的“第三方访问权限”里撤销 Aetrix 的授权，撤销后本站
          立即无法再访问你的盘。
        </li>
        <li>
          <strong>联系开发者：</strong>开发者联系方式见 Google OAuth 同意屏幕上的开发者信息。
        </li>
      </ol>
      <h2 class="privacy-subtitle">Data Protection and Security Measures</h2>
      <p class="privacy-note privacy-section-note">
        以下为数据保护与安全措施说明（英文版）：
      </p>
      <p class="privacy-en-lead">
        We protect the Google user data we access — in particular your
        OAuth tokens — with the following measures:
      </p>
      <ol class="privacy-list">
        <li>
          <strong>Encryption at rest.</strong> Your Google access token and refresh
          token are never stored in plaintext. They are stored only as a
          sealed (encrypted) value, produced with Fernet authenticated
          encryption; the sealing key is derived from a server-side secret
          that is kept outside the database. If the sealed value cannot be
          decrypted, the credential is treated as invalid rather than used.
        </li>
        <li>
          <strong>Tokens stay on the server.</strong> Your tokens are held and used
          only on our server. They are never sent to your browser, never
          rendered in any page, and the binding information our API
          returns is limited to non-secret metadata such as the account
          label and the names of the scopes you granted.
        </li>
        <li>
          <strong>Encryption in transit.</strong> All communication between Aetrix and
          Google (authorization, token exchange, refresh, revocation, and
          Drive API calls) uses HTTPS endpoints.
        </li>
        <li>
          <strong>Hardened authorization flow.</strong> The OAuth flow uses PKCE
          (S256), a short-lived state value, and a nonce that is stored
          only as a hash and compared in constant time; a callback whose
          nonce or session user does not match is rejected and the state
          is discarded.
        </li>
        <li>
          <strong>Least privilege.</strong> We request only the minimum scopes the
          features need — read-only access plus access to files the app
          itself creates — and nothing more.
        </li>
        <li>
          <strong>Per-user isolation.</strong> Each user's credential is stored against
          that user's own account row and is only ever used for that user.
          Operations that act on a binding verify it belongs to the
          requesting user; a binding belonging to someone else is treated
          as non-existent.
        </li>
        <li>
          <strong>Revocation when you disconnect.</strong> When you unbind your Drive
          in Aetrix, we (a) revoke the refresh token with Google's
          revocation endpoint, (b) delete the stored credential, and
          (c) remove the "anyone with the link can view" sharing that
          playback had placed on your transfer copies — all as part of the
          same disconnect action. You can also revoke Aetrix's access at
          any time from your Google Account's third-party access page.
        </li>
        <li>
          <strong>Time-limited link sharing for playback.</strong> The only sharing we
          ever set is "anyone with the link can view" on the transfer
          copies the app itself created in your Drive, so your player can
          stream them directly from Google. This sharing is automatically
          removed after 2 hours of inactivity, and immediately when you
          unbind (see 7). We never change sharing on any other file in
          your Drive.
        </li>
      </ol>
    </div>
  </main>
</template>

<style scoped>
.privacy-page {
  min-height: 100vh;
  min-height: 100dvh;
  display: flex;
  justify-content: center;
  padding: 5rem 1rem calc(3rem + var(--au-dock-space));
}

.privacy-inner {
  width: 100%;
  max-width: 44rem;
  animation: au-fade-up var(--au-fade-in) var(--au-ease) both;
}

.privacy-eyebrow {
  margin: 0 0 0.75rem;
}

.privacy-title {
  margin: 0 0 0.75rem;
  font-size: 1.375rem;
  font-weight: 700;
  color: var(--au-text);
}

.privacy-note {
  margin: 0 0 2rem;
  font-size: 0.875rem;
  color: var(--au-text-3);
}

.privacy-list {
  margin: 0;
  padding-left: 1.5rem;
  display: flex;
  flex-direction: column;
  gap: 1.25rem;
  font-size: 0.9375rem;
  line-height: 1.8;
  color: var(--au-text);
}

.privacy-list strong {
  font-weight: 700;
}

.privacy-subtitle {
  margin: 2.5rem 0 0.75rem;
  font-size: 1.125rem;
  font-weight: 700;
  color: var(--au-text);
}

.privacy-section-note {
  margin-bottom: 1.25rem;
}

.privacy-en-lead {
  margin: 0 0 1.25rem;
  font-size: 0.9375rem;
  line-height: 1.8;
  color: var(--au-text);
}

@media (prefers-reduced-motion: reduce) {
  .privacy-inner { animation: none; }
}
</style>
