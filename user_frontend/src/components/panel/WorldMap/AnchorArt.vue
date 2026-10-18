<template>
  <div class="anchor-art-wrap" aria-hidden="true">
    <div v-if="art === 'cinema'" class="anchor-art"><svg class="art-svg" viewBox="-78 -164 156 184" fill="none" aria-hidden="true"><g transform="translate(0,0)">
  <ellipse cx="0" cy="2" rx="70" ry="12" fill="#2A2620" opacity="0.13"/>

  <!-- 坡屋顶 -->
  <path d="M-64 -108 L0 -156 L64 -108 Z" fill="#D9482B" stroke="#a06c24" stroke-width="2.2" stroke-linejoin="round"/>
  <path d="M-54 -108 L0 -149 L0 -122 Z" fill="#FFFDF6" opacity="0.15"/>

  <!-- 墙体 -->
  <rect x="-50" y="-110" width="100" height="110" fill="#FFFDF6" stroke="#c9b98f" stroke-width="2.2"/>

  <!-- 圆形胶片窗 -->
  <circle cx="0" cy="-72" r="24" fill="#2e3a5c" stroke="#c9b98f" stroke-width="2.2"/>
  <circle cx="12" cy="-72" r="3.4" fill="#e8a84a" stroke="#a06c24" stroke-width="1.5"/>
  <circle cx="6" cy="-61.6" r="3.4" fill="#e8a84a" stroke="#a06c24" stroke-width="1.5"/>
  <circle cx="-6" cy="-61.6" r="3.4" fill="#e8a84a" stroke="#a06c24" stroke-width="1.5"/>
  <circle cx="-12" cy="-72" r="3.4" fill="#e8a84a" stroke="#a06c24" stroke-width="1.5"/>
  <circle cx="-6" cy="-82.4" r="3.4" fill="#e8a84a" stroke="#a06c24" stroke-width="1.5"/>
  <circle cx="6" cy="-82.4" r="3.4" fill="#e8a84a" stroke="#a06c24" stroke-width="1.5"/>

  <!-- 纸色小门 -->
  <rect x="-15" y="-38" width="30" height="38" fill="#F6F1E7" stroke="#c9b98f" stroke-width="2.2"/>
  <circle cx="9" cy="-19" r="2.2" fill="#8a6d3b"/>

  <!-- 门槛 -->
  <rect x="-22" y="-3" width="44" height="6" rx="1.5" fill="#d9cba6" stroke="#c9b98f" stroke-width="1.5"/>

  <!-- 夜景 -->
  <g class="nl" display="none">
    <circle cx="0" cy="-72" r="60" fill="#FFD27A" opacity="0.3"/>
    <circle cx="0" cy="-72" r="24" fill="#FFD27A" stroke="#e8a84a" stroke-width="2.2"/>
    <rect x="-1.8" y="-38" width="3.6" height="38" fill="#FFD27A" opacity="0.85"/>
    <rect x="-22" y="-3" width="44" height="6" rx="1.5" fill="#FFD27A" opacity="0.35"/>
  </g>
</g></svg></div>
    <div v-else-if="art === 'parcels'" class="anchor-art"><svg class="art-svg" viewBox="-72 -138 144 158" fill="none" aria-hidden="true"><g transform="translate(0,0)">
    <ellipse cx="0" cy="2" rx="52" ry="12" fill="#2A2620" opacity="0.13"/>

    <!-- 墙体 -->
    <rect x="-52" y="-78" width="104" height="78" fill="#D8CDB8" stroke="#8a6d3b" stroke-width="2.2"/>

    <!-- 烟囱 -->
    <rect x="22" y="-124" width="13" height="26" fill="#c9b98f" stroke="#8a6d3b" stroke-width="2.2"/>

    <!-- 屋顶 -->
    <polygon points="-64,-78 64,-78 0,-130" fill="#c9b98f" stroke="#8a6d3b" stroke-width="2.2" stroke-linejoin="round"/>
    <polygon points="-58,-80 0,-124 -9,-124 -58,-88" fill="#FFFDF6" opacity="0.15"/>

    <!-- 门 -->
    <rect x="-44" y="-48" width="26" height="48" fill="#c9b98f" stroke="#8a6d3b" stroke-width="2.2"/>
    <circle cx="-38" cy="-24" r="1.8" fill="#8a6d3b"/>

    <!-- 窗 -->
    <rect x="18" y="-66" width="26" height="22" rx="2" fill="#FFD27A" stroke="#8a6d3b" stroke-width="2.2"/>
    <line x1="31" y1="-66" x2="31" y2="-44" stroke="#8a6d3b" stroke-width="1.5"/>
    <line x1="18" y1="-55" x2="44" y2="-55" stroke="#8a6d3b" stroke-width="1.5"/>

    <!-- 纸箱 A -->
    <rect x="-50" y="-22" width="28" height="22" fill="#FFFDF6" stroke="#6B6257" stroke-width="1.8"/>
    <line x1="-36" y1="-22" x2="-36" y2="0" stroke="#6B6257" stroke-width="1.6"/>
    <line x1="-50" y1="-11" x2="-22" y2="-11" stroke="#6B6257" stroke-width="1.6"/>

    <!-- 纸箱 B -->
    <rect x="-22" y="-20" width="26" height="20" fill="#FFFDF6" stroke="#6B6257" stroke-width="1.8"/>
    <line x1="-9" y1="-20" x2="-9" y2="0" stroke="#6B6257" stroke-width="1.6"/>
    <line x1="-22" y1="-10" x2="4" y2="-10" stroke="#6B6257" stroke-width="1.6"/>

    <!-- 纸箱 C -->
    <rect x="-42" y="-44" width="26" height="22" fill="#FFFDF6" stroke="#6B6257" stroke-width="1.8"/>
    <line x1="-29" y1="-44" x2="-29" y2="-22" stroke="#6B6257" stroke-width="1.6"/>
    <line x1="-42" y1="-33" x2="-16" y2="-33" stroke="#6B6257" stroke-width="1.6"/>

    <!-- 火漆章 -->
    <circle cx="-29" cy="-33" r="5.5" fill="#D9482B" stroke="#a06c24" stroke-width="1"/>
    <polygon points="-29,-36.4 -28.12,-34.21 -25.77,-34.05 -27.57,-32.54 -27,-30.25 -29,-31.5 -31,-30.25 -30.43,-32.54 -32.23,-34.05 -29.88,-34.21" fill="#e8a84a"/>

    <g class="nl" display="none">
      <circle cx="31" cy="-55" r="26" fill="#FFD27A" opacity="0.28"/>
      <circle cx="-29" cy="-33" r="13" fill="#D9482B" opacity="0.16"/>
      <circle cx="-29" cy="-33" r="6" fill="#FFD27A" opacity="0.32"/>
    </g>
  </g></svg></div>
    <div v-else-if="art === 'lighthouse'" class="anchor-art"><svg class="art-svg" viewBox="-52 -238 104 258" fill="none" aria-hidden="true"><g transform="translate(0,0)">
  <ellipse cx="0" cy="2" rx="44" ry="12" fill="#2A2620" opacity="0.13"/>

  <!-- 塔身 -->
  <path d="M -30 0 L -13 -190 L 13 -190 L 30 0 Z" fill="#FFFDF6" stroke="#c9b98f" stroke-width="2.2" stroke-linejoin="round"/>

  <!-- 两道朱红横纹 -->
  <path d="M -26.4 -40 L 26.4 -40 L 25.1 -55 L -25.1 -55 Z" fill="#D9482B"/>
  <path d="M -22 -90 L 22 -90 L 20.6 -105 L -20.6 -105 Z" fill="#D9482B"/>

  <!-- 塔身左上高光细线 -->
  <line x1="-24" y1="-14" x2="-10.5" y2="-178" stroke="#d9cba6" stroke-width="1.6" opacity="0.85"/>

  <!-- 灯室 -->
  <rect x="-16" y="-212" width="32" height="28" rx="3" fill="#2e3a5c" stroke="#c9b98f" stroke-width="2"/>
  <circle cx="0" cy="-198" r="5" fill="#a8c6dd"/>
  <path d="M -20 -212 L 20 -212 L 0 -221 Z" fill="#2e3a5c" stroke="#c9b98f" stroke-width="2" stroke-linejoin="round"/>
  <circle cx="0" cy="-225" r="4.5" fill="#e8a84a" stroke="#a06c24" stroke-width="1.6"/>

  <g class="nl" display="none">
    <!-- 暖金光束 -->
    <path d="M -16 -198 L -80 -236 L -80 -212 Z" fill="#FFD27A" opacity="0.25"/>
    <!-- 塔身暖窗 -->
    <rect x="-6.5" y="-142" width="13" height="17" rx="2" fill="#FFD27A"/>
  </g>
</g></svg></div>
    <div v-else-if="art === 'shop'" class="anchor-art"><svg class="art-svg" viewBox="-56 -106 112 126" fill="none" aria-hidden="true"><g transform="translate(0,0)">
  <ellipse cx="0" cy="2" rx="48" ry="12" fill="#2A2620" opacity="0.13"/>
  <rect x="-32" y="-52" width="64" height="52" fill="#93ad8b" stroke="#4A7C59" stroke-width="2.2"/>
  <path d="M -38 -52 L 0 -78 L 38 -52 Z" fill="#F6F1E7" stroke="#4A7C59" stroke-width="2.2" stroke-linejoin="round"/>
  <path d="M -35 -53.5 L -1 -76.5 L -1 -53.5 Z" fill="#FFFDF6" opacity="0.15"/>
  <rect x="-9" y="-34" width="18" height="34" fill="#d9cba6" stroke="#6B6257" stroke-width="2"/>
  <circle cx="4" cy="-17" r="1.6" fill="#6B6257"/>
  <rect x="-24" y="-42" width="12" height="12" fill="#a8c6dd" stroke="#2e3a5c" stroke-width="1.8"/>
  <rect x="16" y="-18" width="14" height="18" rx="2" fill="#d9cba6" stroke="#8a6d3b" stroke-width="1.8"/>
  <line x1="16" y1="-13" x2="30" y2="-13" stroke="#2A2620" stroke-width="1.4"/>
  <line x1="16" y1="-6" x2="30" y2="-6" stroke="#2A2620" stroke-width="1.4"/>
  <line x1="0" y1="-78" x2="0" y2="-98" stroke="#2A2620" stroke-width="1.8"/>
  <path d="M 0 -98 L 16 -92 L 0 -86 Z" fill="#e8a84a" stroke="#a06c24" stroke-width="1.5" stroke-linejoin="round"/>
  <g class="nl" display="none">
    <circle cx="-18" cy="-36" r="11" fill="#FFD27A" opacity="0.55"/>
    <circle cx="7" cy="-84" r="9" fill="#FFD27A" opacity="0.3"/>
    <circle cx="7" cy="-84" r="4" fill="#FFD27A" opacity="0.9"/>
  </g>
</g></svg></div>
    <div v-else-if="art === 'collector'" class="anchor-art"><svg class="art-svg" viewBox="-60 -103 138 122" fill="none" aria-hidden="true"><g transform="translate(0,0)">
  <ellipse cx="0" cy="2" rx="52" ry="11" fill="#2A2620" opacity="0.13"/>

  <!-- 屋身 -->
  <path d="M -23 -56 L -23 -7 Q -23 0 -16 0 L 16 0 Q 23 0 23 -7 L 23 -56 Z"
        fill="#FFFDF6" stroke="#c9b98f" stroke-width="2.2"/>

  <!-- 小圆拱门 -->
  <path d="M -10 0 L -10 -15 A 10 10 0 0 1 10 -15 L 10 0 Z" fill="#8a6d3b"/>

  <!-- 蘑菇盖 -->
  <path d="M -42 -56 A 42 36 0 0 1 42 -56 Z"
        fill="#D9482B" stroke="#8f2f1f" stroke-width="2.2"/>
  <circle cx="-19" cy="-74" r="3.6" fill="#FFFDF6"/>
  <circle cx="0" cy="-83" r="3.6" fill="#FFFDF6"/>
  <circle cx="19" cy="-74" r="3.6" fill="#FFFDF6"/>
  <ellipse cx="-10" cy="-84" rx="15" ry="6.5" fill="#FFFDF6" opacity="0.15"
           transform="rotate(-20 -10 -84)"/>

  <!-- 小金徽章 -->
  <circle cx="56" cy="-18" r="14" fill="#e8a84a" stroke="#a06c24" stroke-width="2"/>
  <path d="M56,-24.5 L57.65,-20.27 L62.18,-20.01 L58.66,-17.13 L59.82,-12.74 L56,-15.2 L52.18,-12.74 L53.34,-17.13 L49.82,-20.01 L54.35,-20.27 Z"
        fill="#2A2620"/>

  <!-- 门缝暖光 -->
  <g class="nl" display="none">
    <ellipse cx="0" cy="-1" rx="15" ry="6" fill="#FFD27A" opacity="0.3"/>
    <rect x="-9" y="-3" width="18" height="3" rx="1.5" fill="#FFD27A"/>
  </g>
</g>
</svg></div>
    <div v-else-if="art === 'post'" class="anchor-art"><svg class="art-svg" viewBox="-60 -124 120 144" fill="none" aria-hidden="true"><g transform="translate(0,0)">
  <ellipse cx="0" cy="2" rx="44" ry="12" fill="#2A2620" opacity="0.13"/>

  <!-- 小木屋墙体 -->
  <rect x="-40" y="-60" width="80" height="60" fill="#d9cba6" stroke="#8a6d3b" stroke-width="2.2" stroke-linejoin="round"/>

  <!-- 坡屋顶 -->
  <polygon points="-52,-56 0,-88 52,-56" fill="#F6F1E7" stroke="#8a6d3b" stroke-width="2.2" stroke-linejoin="round"/>
  <polygon points="-36.4,-65.6 -10.4,-81.6 -7.8,-77.3 -33.8,-61.3" fill="#FFFDF6" opacity="0.15"/>

  <!-- 门 -->
  <rect x="-18" y="-36" width="20" height="36" fill="#c9b98f" stroke="#8a6d3b" stroke-width="1.8" stroke-linejoin="round"/>
  <circle cx="0" cy="-19" r="1.8" fill="#e8a84a"/>

  <!-- 纸窗 -->
  <rect x="12" y="-46" width="20" height="18" fill="#FFFDF6" stroke="#8a6d3b" stroke-width="1.8" stroke-linejoin="round"/>
  <line x1="22" y1="-46" x2="22" y2="-28" stroke="#8a6d3b" stroke-width="1.5"/>
  <line x1="12" y1="-37" x2="32" y2="-37" stroke="#8a6d3b" stroke-width="1.5"/>

  <!-- 门旁小信封 -->
  <rect x="-34" y="-32" width="12" height="9" fill="#FFFDF6" stroke="#6B6257" stroke-width="1.5" stroke-linejoin="round"/>
  <polyline points="-34,-32 -28,-27 -22,-32" fill="none" stroke="#2A2620" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>

  <!-- 屋顶猫头鹰 -->
  <circle cx="0" cy="-100" r="12" fill="#d9cba6" stroke="#8a6d3b" stroke-width="2.2"/>
  <polygon points="-7,-108.5 -5,-116 -2,-110.5" fill="#d9cba6" stroke="#8a6d3b" stroke-width="1.8" stroke-linejoin="round"/>
  <polygon points="7,-108.5 5,-116 2,-110.5" fill="#d9cba6" stroke="#8a6d3b" stroke-width="1.8" stroke-linejoin="round"/>
  <circle cx="-4.5" cy="-103" r="4.2" fill="#FFFDF6" stroke="#8a6d3b" stroke-width="1.5"/>
  <circle cx="4.5" cy="-103" r="4.2" fill="#FFFDF6" stroke="#8a6d3b" stroke-width="1.5"/>
  <circle cx="-4.5" cy="-103" r="1.7" fill="#2A2620"/>
  <circle cx="4.5" cy="-103" r="1.7" fill="#2A2620"/>
  <polygon points="0,-97.5 -2.5,-94.5 2.5,-94.5" fill="#e8a84a"/>

  <g class="nl" display="none">
    <circle cx="22" cy="-37" r="20" fill="#FFD27A" opacity="0.18"/>
    <circle cx="22" cy="-37" r="14" fill="#FFD27A" opacity="0.24"/>
    <rect x="12" y="-46" width="20" height="18" fill="#FFD27A" stroke="#8a6d3b" stroke-width="1.8" stroke-linejoin="round"/>
    <line x1="22" y1="-46" x2="22" y2="-28" stroke="#c97e2c" stroke-width="1.5"/>
    <line x1="12" y1="-37" x2="32" y2="-37" stroke="#c97e2c" stroke-width="1.5"/>
  </g>
</g></svg></div>
    <div v-else-if="art === 'gamehall'" class="anchor-art"><svg class="art-svg" viewBox="-60 -133 120 153" fill="none" aria-hidden="true"><g transform="translate(0,0)">
  <!-- 接地阴影 -->
  <ellipse cx="0" cy="2" rx="52" ry="12" fill="#2A2620" opacity="0.13"/>
  
  <!-- 建筑主体 -->
  <rect x="-40" y="-100" width="80" height="100" rx="4" fill="#FFFDF6" stroke="#c9b98f" stroke-width="2.2"/>
  
  <!-- 屋顶 -->
  <path d="M -52,-100 Q 0,-135 52,-100 Z" fill="#a8c6dd" stroke="#2e3a5c" stroke-width="2" stroke-linejoin="round"/>
  <!-- 屋顶提亮 -->
  <path d="M -48,-100 Q -20,-112 0,-125 L 0,-100 Z" fill="#FFFDF6" opacity="0.4"/>
  
  <!-- 门 -->
  <rect x="-15" y="-50" width="30" height="50" rx="3" fill="#6B6257" stroke="#2A2620" stroke-width="1.8"/>
  <!-- 门把手 -->
  <circle cx="8" cy="-25" r="2" fill="#e8a84a"/>
  
  <!-- 门徽记（金色圆 + 墨色骰子） -->
  <circle cx="0" cy="-30" r="7" fill="#e8a84a" stroke="#a06c24" stroke-width="1.5"/>
  <rect x="-3" y="-33" width="6" height="6" rx="1.5" fill="#2A2620"/>
  <circle cx="-1" cy="-31" r="0.8" fill="#e8a84a"/>
  <circle cx="1" cy="-31" r="0.8" fill="#e8a84a"/>
  <circle cx="0" cy="-30" r="0.8" fill="#e8a84a"/>
  <circle cx="-1" cy="-29" r="0.8" fill="#e8a84a"/>
  <circle cx="1" cy="-29" r="0.8" fill="#e8a84a"/>
  
  <!-- 窗户 -->
  <rect x="-28" y="-80" width="12" height="14" rx="2" fill="#FFFDF6" stroke="#2A2620" stroke-width="1.5"/>
  <line x1="-22" y1="-80" x2="-22" y2="-66" stroke="#2A2620" stroke-width="1"/>
  <line x1="-28" y1="-73" x2="-16" y2="-73" stroke="#2A2620" stroke-width="1"/>
  <rect x="16" y="-80" width="12" height="14" rx="2" fill="#FFFDF6" stroke="#2A2620" stroke-width="1.5"/>
</g>
<g class="nl" display="none">
  <ellipse cx="-28" cy="-80" rx="11" ry="12.5" fill="#FFD27A" opacity=".3"/>
  <ellipse cx="16" cy="-80" rx="11" ry="12.5" fill="#FFD27A" opacity=".3"/>
  <rect x="-34" y="-87" width="12" height="14" rx="2.5" fill="#FFD27A"/>
  <rect x="10" y="-87" width="12" height="14" rx="2.5" fill="#FFD27A"/>
  <circle cx="-44" cy="-96" r="10" fill="#FFD27A" opacity=".3"/>
  <circle cx="44" cy="-96" r="10" fill="#FFD27A" opacity=".3"/>
  <circle cx="-44" cy="-96" r="4" fill="#FFD27A"/>
  <circle cx="44" cy="-96" r="4" fill="#FFD27A"/>
</g></svg></div>
    <div v-else-if="art === 'stele'" class="anchor-art"><svg class="art-svg" viewBox="-48 -204 96 222" fill="none" aria-hidden="true"><g transform="translate(0,0)">
  <ellipse cx="0" cy="2" rx="40" ry="10" fill="#2A2620" opacity="0.13"/>
  <rect x="-35" y="-170" width="70" height="170" rx="10" fill="#2e3a5c" stroke="#c97e2c" stroke-width="2"/>
  <rect x="-29" y="-156" width="8" height="112" rx="4" fill="#FFFDF6" opacity="0.12"/>
  <path fill="#e8a84a" d="M-24.5 -120a3.5 3.5 0 1 0 7 0a3.5 3.5 0 1 0-7 0M-10.5 -120a3.5 3.5 0 1 0 7 0a3.5 3.5 0 1 0-7 0M3.5 -120a3.5 3.5 0 1 0 7 0a3.5 3.5 0 1 0-7 0M17.5 -120a3.5 3.5 0 1 0 7 0a3.5 3.5 0 1 0-7 0M-24.5 -100a3.5 3.5 0 1 0 7 0a3.5 3.5 0 1 0-7 0M-10.5 -100a3.5 3.5 0 1 0 7 0a3.5 3.5 0 1 0-7 0M3.5 -100a3.5 3.5 0 1 0 7 0a3.5 3.5 0 1 0-7 0M17.5 -100a3.5 3.5 0 1 0 7 0a3.5 3.5 0 1 0-7 0"/>
  <ellipse cx="0" cy="-170" rx="16" ry="6" fill="#FFFDF6" opacity="0.35"/>
  <polygon points="0,-196 14,-186 0,-176 -14,-186" fill="#a8c6dd" stroke="#2e3a5c" stroke-width="1.5"/>
  <g class="nl" display="none">
    <circle cx="0" cy="-186" r="16" fill="#a8c6dd" opacity="0.4"/>
    <circle cx="-21" cy="-120" r="2" fill="#FFD27A" opacity="0.5"/>
    <circle cx="-7" cy="-120" r="2" fill="#FFD27A" opacity="0.5"/>
    <circle cx="7" cy="-120" r="2" fill="#FFD27A" opacity="0.5"/>
    <circle cx="21" cy="-120" r="2" fill="#FFD27A" opacity="0.5"/>
    <circle cx="-21" cy="-100" r="2" fill="#FFD27A" opacity="0.5"/>
    <circle cx="-7" cy="-100" r="2" fill="#FFD27A" opacity="0.5"/>
    <circle cx="7" cy="-100" r="2" fill="#FFD27A" opacity="0.5"/>
    <circle cx="21" cy="-100" r="2" fill="#FFD27A" opacity="0.5"/>
  </g>
</g>
</svg></div>
    <div v-else-if="art === 'board'" class="anchor-art"><svg class="art-svg" viewBox="-52 -108 104 128" fill="none" aria-hidden="true"><g transform="translate(0,0)">
  <ellipse cx="0" cy="2" rx="44" ry="12" fill="#2A2620" opacity="0.13"/>

  <rect x="-31" y="-88" width="10" height="88" rx="3" fill="#c9b98f" stroke="#8a6d3b" stroke-width="2.2"/>
  <rect x="21" y="-88" width="10" height="88" rx="3" fill="#c9b98f" stroke="#8a6d3b" stroke-width="2.2"/>

  <rect x="-44" y="-76" width="88" height="46" rx="5" fill="#d9cba6" stroke="#8a6d3b" stroke-width="2.2"/>

  <line x1="-32" y1="-61" x2="18" y2="-61" stroke="#2A2620" stroke-width="2" stroke-linecap="round" opacity="0.72"/>
  <line x1="-32" y1="-49" x2="4" y2="-49" stroke="#2A2620" stroke-width="2" stroke-linecap="round" opacity="0.72"/>

  <line x1="-37" y1="-69" x2="-10" y2="-69" stroke="#FFFDF6" stroke-width="2" stroke-linecap="round" opacity="0.2"/>

  <g>
    <path d="M0 -76 L0 -100" stroke="#a06c24" stroke-width="2" stroke-linecap="round"/>
    <ellipse cx="-3" cy="-93" rx="3" ry="5" fill="#e8a84a" transform="rotate(-25 -3 -93)"/>
    <ellipse cx="3" cy="-93" rx="3" ry="5" fill="#c97e2c" transform="rotate(25 3 -93)"/>
    <ellipse cx="-3" cy="-87" rx="3" ry="5" fill="#c97e2c" transform="rotate(-25 -3 -87)"/>
    <ellipse cx="3" cy="-87" rx="3" ry="5" fill="#e8a84a" transform="rotate(25 3 -87)"/>
</g>
</g>
<g class="nl" display="none">
  <ellipse cx="0" cy="-52" rx="46" ry="30" fill="#FFD27A" opacity=".22"/>
  <path d="M-30 -104l16 8-16 8z" fill="#e8a84a"/>
  <path d="M30 -104l-16 8 16 8z" fill="#c97e2c"/>
  <circle cx="-38" cy="-102" r="6" fill="#FFD27A" opacity=".3"/>
  <circle cx="38" cy="-102" r="6" fill="#FFD27A" opacity=".3"/>
  <circle cx="-38" cy="-102" r="3" fill="#FFD27A"/>
  <circle cx="38" cy="-102" r="3" fill="#FFD27A"/>
</g></svg></div>
    <div v-else-if="art === 'wall'" class="anchor-art"><svg class="art-svg" viewBox="-76 -170 148 190" fill="none" aria-hidden="true"><g transform="translate(0,0)">
  <ellipse cx="0" cy="2" rx="56" ry="12" fill="#2A2620" opacity="0.13"/>
  <path d="M-9 0 C-7 -24 -6 -46 -5 -70 L5 -70 C6 -46 7 -24 9 0 Z" fill="#8a6d3b"/>
  <path d="M-4 -6 C-3 -28 -3 -48 -3 -66" stroke="#c9b98f" stroke-width="1.5" fill="none" opacity="0.7"/>
  <circle cx="-34" cy="-94" r="34" fill="#7d9b76"/>
  <circle cx="32" cy="-98" r="32" fill="#7d9b76"/>
  <circle cx="4" cy="-90" r="30" fill="#93ad8b"/>
  <circle cx="-4" cy="-126" r="36" fill="#93ad8b"/>
  <ellipse cx="-10" cy="-150" rx="20" ry="9" fill="#FFFDF6" opacity="0.12"/>
</g>
<g class="nl" display="none">
  <circle cx="-30" cy="-70" r="2.5" fill="#FFD27A"/>
  <circle cx="-30" cy="-70" r="7" fill="#FFD27A" opacity=".35"/>
  <circle cx="28" cy="-74" r="2.5" fill="#FFD27A"/>
  <circle cx="28" cy="-74" r="7" fill="#FFD27A" opacity=".35"/>
</g></svg></div>
    <div v-else-if="art === 'fortune'" class="anchor-art"><svg class="art-svg" viewBox="-70 -168 140 188" fill="none" aria-hidden="true"><g transform="translate(0,0)">
  <ellipse cx="0" cy="2" rx="46" ry="12" fill="#2A2620" opacity="0.13"/>

  <!-- 小圆地台 -->
  <ellipse cx="0" cy="-1" rx="56" ry="13" fill="#d9cba6" stroke="#8a6d3b" stroke-width="2"/>
  <ellipse cx="0" cy="-3" rx="45" ry="9.5" fill="#c9b98f" opacity="0.45"/>

  <!-- 后柱 -->
  <rect x="-26.5" y="-100" width="5" height="92" rx="1.5" fill="#F6F1E7" stroke="#6B6257" stroke-width="1.8"/>
  <rect x="21.5" y="-100" width="5" height="92" rx="1.5" fill="#F6F1E7" stroke="#6B6257" stroke-width="1.8"/>

  <!-- 签筒 -->
  <rect x="-14" y="-54" width="28" height="46" rx="3" fill="#c9b98f" stroke="#8a6d3b" stroke-width="2.2"/>
  <path d="M-13 -36 h26" stroke="#8a6d3b" stroke-width="1.5" opacity="0.45"/>
  <ellipse cx="0" cy="-54" rx="14" ry="5" fill="#d9cba6"/>

  <!-- 五支签 -->
  <g transform="translate(-9,-52) rotate(-13)">
    <rect x="-1.7" y="-25" width="3.4" height="25" rx="1.2" fill="#FFFDF6" stroke="#6B6257" stroke-width="1.5"/>
    <rect x="-1.7" y="-25" width="3.4" height="4.6" rx="1.2" fill="#D9482B"/>
  </g>
  <g transform="translate(-4.5,-52) rotate(-6)">
    <rect x="-1.7" y="-28" width="3.4" height="28" rx="1.2" fill="#FFFDF6" stroke="#6B6257" stroke-width="1.5"/>
    <rect x="-1.7" y="-28" width="3.4" height="4.6" rx="1.2" fill="#D9482B"/>
  </g>
  <g transform="translate(0,-52) rotate(1)">
    <rect x="-1.7" y="-31" width="3.4" height="31" rx="1.2" fill="#FFFDF6" stroke="#6B6257" stroke-width="1.5"/>
    <rect x="-1.7" y="-31" width="3.4" height="4.6" rx="1.2" fill="#D9482B"/>
  </g>
  <g transform="translate(4.5,-52) rotate(7)">
    <rect x="-1.7" y="-27" width="3.4" height="27" rx="1.2" fill="#FFFDF6" stroke="#6B6257" stroke-width="1.5"/>
    <rect x="-1.7" y="-27" width="3.4" height="4.6" rx="1.2" fill="#D9482B"/>
  </g>
  <g transform="translate(9,-52) rotate(14)">
    <rect x="-1.7" y="-24" width="3.4" height="24" rx="1.2" fill="#FFFDF6" stroke="#6B6257" stroke-width="1.5"/>
    <rect x="-1.7" y="-24" width="3.4" height="4.6" rx="1.2" fill="#D9482B"/>
  </g>

  <ellipse cx="0" cy="-54" rx="14" ry="5" fill="none" stroke="#8a6d3b" stroke-width="2.2"/>

  <!-- 前柱 -->
  <rect x="-49" y="-100" width="6" height="97" rx="2" fill="#FFFDF6" stroke="#6B6257" stroke-width="2"/>
  <rect x="43" y="-100" width="6" height="97" rx="2" fill="#FFFDF6" stroke="#6B6257" stroke-width="2"/>

  <!-- 檐口 -->
  <rect x="-62" y="-103" width="124" height="8" rx="2" fill="#FFFDF6" stroke="#a06c24" stroke-width="2"/>

  <!-- 金色小尖顶 -->
  <path d="M0 -156 L60 -103 L-60 -103 Z" fill="#e8a84a" stroke="#a06c24" stroke-width="2.5" stroke-linejoin="round"/>
  <path d="M0 -156 L60 -103 L0 -103 Z" fill="#c97e2c" opacity="0.4"/>
  <path d="M0 -152 L-52 -105" stroke="#FFD27A" stroke-width="2.5" stroke-linecap="round" opacity="0.85"/>
  <circle cx="0" cy="-156" r="4.5" fill="#FFD27A" stroke="#a06c24" stroke-width="1.8"/>

  <!-- 夜间暖光灯笼 -->
  <g class="nl" display="none">
    <line x1="30" y1="-103" x2="30" y2="-88" stroke="#6B6257" stroke-width="1.5"/>
    <circle cx="30" cy="-78" r="17" fill="#FFD27A" opacity="0.22"/>
    <circle cx="30" cy="-78" r="9" fill="#FFD27A" stroke="#c97e2c" stroke-width="1.8"/>
</g>
</g>
</svg></div>
  </div>
</template>

<script setup lang="ts">
/**
 * 建筑插画：从旧 WorldMap.vue 逐字搬出的 11 组建筑 SVG（未改一笔）。
 * 桌面建筑锚点与手机长卷轴大插画锚点共用，避免两份拷贝。
 * 夜灯 .nl 组显隐沿用旧口径（dp-night 下显形）。
 */
defineProps<{
  art:
    | 'cinema'
    | 'parcels'
    | 'lighthouse'
    | 'shop'
    | 'collector'
    | 'post'
    | 'gamehall'
    | 'stele'
    | 'board'
    | 'wall'
    | 'fortune'
}>()
</script>

<style scoped>
.anchor-art-wrap {
  display: block;
  width: 100%;
}
.anchor-art-wrap :deep(.art-svg) {
  width: 100%;
  height: auto;
  display: block;
}
.anchor-art-wrap :deep(.nl) {
  display: none;
}
</style>

<style>
/* 夜灯层：部件自带 .nl（暖窗光/光晕），只在夜里显形 —— 与旧 WorldMap.vue 口径一致 */
.world-root.dp-night .anchor-art-wrap .nl {
  display: inline;
}
</style>
