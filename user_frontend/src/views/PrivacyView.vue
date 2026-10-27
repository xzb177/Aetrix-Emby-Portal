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

@media (prefers-reduced-motion: reduce) {
  .privacy-inner { animation: none; }
}
</style>
