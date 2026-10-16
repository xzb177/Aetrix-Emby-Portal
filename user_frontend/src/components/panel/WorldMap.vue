<template>
  <div class="world-root" :class="[`dp-${daypart}`, { 'is-game': variant === 'game' }]">
    <div class="game-goalbar" v-if="variant === 'game'">
      <div class="m-goal-text">今日探险 <b>{{ questDoneCount }}/{{ quests.length }}</b><template v-if="questNextLabel"> · 还差：{{ questNextLabel }}</template><template v-else> · 全部完成 ✓</template></div>
      <div class="m-goal-bar"><i :style="{ width: questPercent + '%' }"></i></div>
    </div>
    <div class="world">
      <svg class="scene scene-desktop" viewBox="0 0 1568 882" fill="none" preserveAspectRatio="xMidYMid slice">
<defs><linearGradient id="paper" x1="0" y1="0" x2=".15" y2="1"><stop offset="0" stop-color="#f3ead3"/><stop offset=".46" stop-color="#F6F1E7"/><stop offset="1" stop-color="#efe4cb"/></linearGradient><filter id="soft" x="-40%" y="-60%" width="180%" height="220%"><feGaussianBlur stdDeviation="2.6"/></filter><filter id="bloom" x="-90%" y="-90%" width="280%" height="280%"><feGaussianBlur stdDeviation="7"/></filter></defs><g class="scene-terrain"><rect width="1568" height="882" fill="url(#paper)"/><circle cx="125.44" cy="88.2" r="30" fill="#f2c063" class="scene-sun" opacity=".85"/><circle cx="125.44" cy="88.2" r="44" fill="#f2c063" class="scene-sun" opacity=".22"/><g fill="#FFFDF6" opacity=".85"><ellipse cx="722" cy="56" rx="52" ry="17"/><ellipse cx="750" cy="46" rx="32" ry="14"/><ellipse cx="692" cy="51" rx="26" ry="12"/></g><g fill="#FFFDF6" opacity=".85"><ellipse cx="1055" cy="50" rx="52" ry="17"/><ellipse cx="1083" cy="40" rx="32" ry="14"/><ellipse cx="1025" cy="45" rx="26" ry="12"/></g><g fill="#FFFDF6" opacity=".85"><ellipse cx="938" cy="73" rx="52" ry="17"/><ellipse cx="966" cy="63" rx="32" ry="14"/><ellipse cx="908" cy="68" rx="26" ry="12"/></g><path d="M-20 459 Q 392 388 862 459 T 1588 423 L1588 902 L-20 902 Z" fill="#e9dfc6" opacity=".8"/><path d="M-20 582 Q 470 512 941 582 T 1588 547 L1588 902 L-20 902 Z" fill="#e2d3ae" opacity=".75"/><path d="M125 892 Q 470 617 815 688 T 1490 547" fill="none" stroke="#a8c6dd" stroke-width="24" stroke-linecap="round" opacity=".8"/><path d="M125 892 Q 470 617 815 688 T 1490 547" fill="none" stroke="#c3d9ea" stroke-width="9" stroke-linecap="round" opacity=".8"/></g><g class="scene-trails"><path d="M272,414 Q806,360 1011,374 Q1216,388 1239,463 Q1262,538 1157,559 Q1052,580 966,650 Q880,720 723,724 Q566,728 433,735 Q300,742 248,671 Q196,600 324,586 Q452,572 576,586 T700,600" fill="none" stroke="#c97e2c" stroke-width="5" stroke-linecap="round" opacity=".45"/><path d="M272,414 Q806,360 1011,374 Q1216,388 1239,463 Q1262,538 1157,559 Q1052,580 966,650 Q880,720 723,724 Q566,728 433,735 Q300,742 248,671 Q196,600 324,586 Q452,572 576,586 T700,600" fill="none" stroke="#a06c24" stroke-width="2.6" stroke-dasharray="2 9" stroke-linecap="round" opacity=".9"/><circle cx="272" cy="414" r="7" fill="#FFFDF6" stroke="#a06c24" stroke-width="2.4"/><circle cx="806" cy="360" r="7" fill="#FFFDF6" stroke="#a06c24" stroke-width="2.4"/><circle cx="1216" cy="388" r="7" fill="#FFFDF6" stroke="#a06c24" stroke-width="2.4"/><circle cx="1262" cy="538" r="7" fill="#FFFDF6" stroke="#a06c24" stroke-width="2.4"/><circle cx="1052" cy="580" r="7" fill="#FFFDF6" stroke="#a06c24" stroke-width="2.4"/><circle cx="880" cy="720" r="7" fill="#FFFDF6" stroke="#a06c24" stroke-width="2.4"/><circle cx="566" cy="728" r="7" fill="#FFFDF6" stroke="#a06c24" stroke-width="2.4"/><circle cx="300" cy="742" r="7" fill="#FFFDF6" stroke="#a06c24" stroke-width="2.4"/><circle cx="196" cy="600" r="7" fill="#FFFDF6" stroke="#a06c24" stroke-width="2.4"/><circle cx="452" cy="572" r="7" fill="#FFFDF6" stroke="#a06c24" stroke-width="2.4"/><circle cx="700" cy="600" r="7" fill="#FFFDF6" stroke="#a06c24" stroke-width="2.4"/></g><g class="scene-veg"><g transform="translate(1416,793) scale(1.10)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(702,388) scale(0.90)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(77,312) scale(1.02)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(1133,465) scale(0.73)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(1205,593) scale(1.00)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(964,806) scale(0.86)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(1171,470) scale(0.96)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(1023,440) scale(0.74)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(745,665) scale(0.97)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(187,286) scale(0.82)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(532,471) scale(0.83)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(857,409) scale(0.75)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(1206,659) scale(1.07)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(1407,595) scale(1.13)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(289,808) scale(0.73)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(659,278) scale(1.06)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(1063,807) scale(1.06)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(1452,565) scale(1.14)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(1155,458) scale(1.14)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(601,372) scale(0.73)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(733,374) scale(1.12)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(1454,385) scale(0.75)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(1380,512) scale(0.85)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(1368,718) scale(1.10)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(1071,727) scale(0.99)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(395,429) scale(0.88)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(1194,606) scale(1.02)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(1276,367) scale(1.05)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(531,607) scale(1.13)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(405,816) scale(0.83)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(878,598) scale(0.89)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(876,560) scale(0.98)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(1113,311) scale(0.95)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(1338,417) scale(1.07)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(291,548) scale(0.97)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(153,682) scale(0.99)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(1069,596) scale(1.02)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(823,468) scale(1.16)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(160,776) scale(1.15)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(781,723) scale(1.07)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(942,464) scale(0.93)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(557,470) scale(1.18)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(545,515) scale(0.97)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(1261,563) scale(1.02)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(415,457) scale(0.84)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(358,449) scale(1.14)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(728,373) scale(1.03)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(697,766) scale(0.90)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(1313,441) scale(1.19)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(986,331) scale(1.12)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(251,755) scale(0.84)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(1143,520) scale(1.07)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g></g><g class="scene-dress"><g transform="translate(1330,150)"><svg xmlns="http://www.w3.org/2000/svg" viewBox="-40 -145 80 165" width="160" height="330">
  <g transform="translate(0,0)">
    <!-- 气球主体 -->
    <path d="M0,-132 C-22,-132 -28,-112 -28,-98 C-28,-82 -14,-70 0,-68 C14,-70 28,-82 28,-98 C28,-112 22,-132 0,-132 Z"
          fill="#FFFDF6" stroke="#c9b98f" stroke-width="2" stroke-linejoin="round"/>
    <!-- 土色竖纹 3 条 -->
    <path d="M0,-131 Q2,-100 0,-68" fill="none" stroke="#d9cba6" stroke-width="1.5" stroke-linecap="round"/>
    <path d="M-14,-123 Q-12,-96 -7,-70" fill="none" stroke="#d9cba6" stroke-width="1.5" stroke-linecap="round"/>
    <path d="M14,-123 Q12,-96 7,-70" fill="none" stroke="#d9cba6" stroke-width="1.5" stroke-linecap="round"/>
    <!-- 小金星 -->
    <path d="M0,-113.5 L1.6,-109.8 L5.5,-109.4 L2.6,-106.7 L3.4,-102.9 L0,-105 L-3.4,-102.9 L-2.6,-106.7 L-5.5,-109.4 L-1.6,-109.8 Z"
          fill="#e8a84a" stroke="#c97e2c" stroke-width="0.8" stroke-linejoin="round"/>
    <!-- 4 根墨色细绳 -->
    <path d="M-13,-72 L-9,-40" fill="none" stroke="#2A2620" stroke-width="1.5" stroke-linecap="round"/>
    <path d="M-5,-68 L-4,-40" fill="none" stroke="#2A2620" stroke-width="1.5" stroke-linecap="round"/>
    <path d="M5,-68 L4,-40" fill="none" stroke="#2A2620" stroke-width="1.5" stroke-linecap="round"/>
    <path d="M13,-72 L9,-40" fill="none" stroke="#2A2620" stroke-width="1.5" stroke-linecap="round"/>
    <!-- 土色小吊篮 -->
    <rect x="-11" y="-40" width="22" height="15" rx="3" fill="#d9cba6" stroke="#8a6d3b" stroke-width="2"/>
    <path d="M-11,-34 L11,-34" fill="none" stroke="#8a6d3b" stroke-width="1.5" stroke-linecap="round"/>
  </g>
</svg></g><g transform="translate(120,780)"><g transform="translate(0,0)">
  <ellipse cx="0" cy="0" rx="16" ry="3.5" fill="#c9b98f" opacity="0.55"/>
  <circle cx="0" cy="-26" r="26" fill="none" stroke="#c97e2c" stroke-width="1.5"/>
  <circle cx="0" cy="-26" r="22" fill="#FFFDF6" stroke="#c9b98f" stroke-width="2"/>
  <circle cx="0" cy="-26" r="13" fill="none" stroke="#d9cba6" stroke-width="1.2"/>
  <path d="M16 -26 L20 -26 M11.31 -14.69 L14.14 -11.86 M0 -10 L0 -6 M-11.31 -14.69 L-14.14 -11.86 M-16 -26 L-20 -26 M-11.31 -37.31 L-14.14 -40.14 M0 -42 L0 -46 M11.31 -37.31 L14.14 -40.14" fill="none" stroke="#2A2620" stroke-width="1.6" stroke-linecap="round"/>
  <path d="M0 -40 L3.5 -26 L0 -12 L-3.5 -26 Z" fill="#D9482B"/>
  <circle cx="0" cy="-26" r="2.6" fill="#2A2620"/>
  <circle cx="0" cy="-41.5" r="2" fill="#FFD27A"/>
</g></g><g transform="translate(950,770)"><g transform="translate(0,0)">
  <ellipse cx="0" cy="2" rx="33" ry="5" fill="#6B6257" opacity="0.18"/>
  <rect x="-32" y="-42" width="64" height="42" rx="7" fill="#d9cba6" stroke="#8a6d3b" stroke-width="2"/>
  <path d="M-30,-39 H30" stroke="#8a6d3b" stroke-width="1.5" opacity="0.45" fill="none"/>
  <path d="M-11,-35 V-6 M11,-35 V-6" stroke="#8a6d3b" stroke-width="1.5" opacity="0.38" fill="none"/>
  <rect x="-6.5" y="-27" width="13" height="15" rx="3" fill="#e8a84a" stroke="#c97e2c" stroke-width="1.5"/>
  <circle cx="0" cy="-20" r="2.2" fill="#a06c24"/>
  <rect x="-30" y="-45.5" width="60" height="3.5" rx="1.7" fill="#FFFDF6" opacity="0.8"/>
  <circle cx="-6" cy="-48" r="1.4" fill="#FFD27A" opacity="0.8"/>
  <circle cx="9" cy="-49" r="1.1" fill="#FFD27A" opacity="0.7"/>
  <g transform="rotate(-25 -28 -40)">
    <rect x="-30" y="-53" width="60" height="13" rx="6" fill="#d9cba6" stroke="#8a6d3b" stroke-width="2"/>
    <rect x="-25" y="-50" width="50" height="7" rx="3.5" fill="#c9b98f"/>
  </g>
</g></g><g transform="translate(950,180)"><svg xmlns="http://www.w3.org/2000/svg" viewBox="-60 -110 120 130" width="120" height="130">
  <g transform="translate(0,0)">
    <ellipse cx="0" cy="-8" rx="27" ry="8" fill="#FFFDF6" opacity="0.5"/>
    <path d="M0 -74 L21 -44 L0 -14 L-21 -44 Z" fill="#a8c6dd" stroke="#2e3a5c" stroke-width="1.5" stroke-linejoin="round"/>
    <path d="M-7 -48 L-1 -66" stroke="#FFFDF6" stroke-width="1.5" stroke-linecap="round" fill="none"/>
    <path d="M4 -52 L7 -58" stroke="#FFFDF6" stroke-width="1.5" stroke-linecap="round" fill="none"/>
    <circle cx="23" cy="-72" r="2.5" fill="#a8c6dd"/>
    <g class="nl" display="none">
      <circle cx="0" cy="-44" r="40" fill="#a8c6dd" opacity="0.4"/>
    </g>
  </g>
</svg></g><g transform="translate(150,250)"><g transform="translate(0,0)">
  <ellipse cx="0" cy="0" rx="9" ry="2.5" fill="#d9cba6" opacity="0.7"/>
  <path d="M0,0 L0,-70" stroke="#2A2620" stroke-width="2.5" stroke-linecap="round"/>
  <circle cx="0" cy="-70" r="2.4" fill="#2A2620"/>
  <path d="M0,-70 Q32,-52 64,-44" fill="none" stroke="#6B6257" stroke-width="1.5" stroke-linecap="round"/>
  <path d="M16,-61.6 L29,-55.7 L22.5,-46 Z" fill="#e8a84a" stroke="#2A2620" stroke-width="1.6" stroke-linejoin="round"/>
  <path d="M32,-54.5 L45,-49.6 L38.5,-40 Z" fill="#D9482B" stroke="#2A2620" stroke-width="1.6" stroke-linejoin="round"/>
  <path d="M48,-48.6 L61,-44.8 L54.5,-35 Z" fill="#a8c6dd" stroke="#2A2620" stroke-width="1.6" stroke-linejoin="round"/>
  <g class="nl" display="none">
    <path d="M54.5,-35 L56,-28" stroke="#6B6257" stroke-width="1.5" stroke-linecap="round"/>
    <circle cx="56" cy="-24" r="8" fill="#FFD27A" opacity="0.28"/>
    <circle cx="56" cy="-24" r="3.4" fill="#FFD27A" stroke="#a06c24" stroke-width="1.4"/>
  </g>
</g></g></g><g class="scene-ff"><g transform="translate(1150,420)"><g class="nl" display="none">
    <circle cx="-34" cy="-72" r="10" fill="#FFD27A" opacity=".45"/>
    <circle cx="-34" cy="-72" r="3.2" fill="#FFD27A"/>
    <circle cx="36" cy="-58" r="9" fill="#FFD27A" opacity=".45"/>
    <circle cx="36" cy="-58" r="3" fill="#FFD27A"/>
    <circle cx="26" cy="-96" r="7.5" fill="#FFD27A" opacity=".45"/>
    <circle cx="26" cy="-96" r="2.5" fill="#FFD27A"/>
  </g>
</g><g transform="translate(1290,560)"><g class="nl" display="none">
    <circle cx="-34" cy="-72" r="10" fill="#FFD27A" opacity=".45"/>
    <circle cx="-34" cy="-72" r="3.2" fill="#FFD27A"/>
    <circle cx="36" cy="-58" r="9" fill="#FFD27A" opacity=".45"/>
    <circle cx="36" cy="-58" r="3" fill="#FFD27A"/>
    <circle cx="26" cy="-96" r="7.5" fill="#FFD27A" opacity=".45"/>
    <circle cx="26" cy="-96" r="2.5" fill="#FFD27A"/>
  </g>
</g><g transform="translate(700,640)"><g class="nl" display="none">
    <circle cx="-34" cy="-72" r="10" fill="#FFD27A" opacity=".45"/>
    <circle cx="-34" cy="-72" r="3.2" fill="#FFD27A"/>
    <circle cx="36" cy="-58" r="9" fill="#FFD27A" opacity=".45"/>
    <circle cx="36" cy="-58" r="3" fill="#FFD27A"/>
    <circle cx="26" cy="-96" r="7.5" fill="#FFD27A" opacity=".45"/>
    <circle cx="26" cy="-96" r="2.5" fill="#FFD27A"/>
  </g>
</g></g>
      </svg>
      <svg class="scene scene-mobile" viewBox="0 0 780 1400" fill="none" preserveAspectRatio="xMidYMid slice">
<defs><linearGradient id="paper" x1="0" y1="0" x2=".15" y2="1"><stop offset="0" stop-color="#f3ead3"/><stop offset=".46" stop-color="#F6F1E7"/><stop offset="1" stop-color="#efe4cb"/></linearGradient><filter id="soft" x="-40%" y="-60%" width="180%" height="220%"><feGaussianBlur stdDeviation="2.6"/></filter><filter id="bloom" x="-90%" y="-90%" width="280%" height="280%"><feGaussianBlur stdDeviation="7"/></filter></defs><g class="scene-terrain"><rect width="780" height="1400" fill="url(#paper)"/><circle cx="62.4" cy="140.0" r="30" fill="#f2c063" class="scene-sun" opacity=".85"/><circle cx="62.4" cy="140.0" r="44" fill="#f2c063" class="scene-sun" opacity=".22"/><g fill="#FFFDF6" opacity=".85"><ellipse cx="359" cy="89" rx="52" ry="17"/><ellipse cx="387" cy="79" rx="32" ry="14"/><ellipse cx="329" cy="84" rx="26" ry="12"/></g><g fill="#FFFDF6" opacity=".85"><ellipse cx="525" cy="79" rx="52" ry="17"/><ellipse cx="553" cy="69" rx="32" ry="14"/><ellipse cx="495" cy="74" rx="26" ry="12"/></g><g fill="#FFFDF6" opacity=".85"><ellipse cx="467" cy="116" rx="52" ry="17"/><ellipse cx="495" cy="106" rx="32" ry="14"/><ellipse cx="437" cy="111" rx="26" ry="12"/></g><path d="M-20 728 Q 195 616 429 728 T 800 672 L800 1420 L-20 1420 Z" fill="#e9dfc6" opacity=".8"/><path d="M-20 924 Q 234 812 468 924 T 800 868 L800 1420 L-20 1420 Z" fill="#e2d3ae" opacity=".75"/><path d="M62 1410 Q 234 980 406 1092 T 741 868" fill="none" stroke="#a8c6dd" stroke-width="24" stroke-linecap="round" opacity=".8"/><path d="M62 1410 Q 234 980 406 1092 T 741 868" fill="none" stroke="#c3d9ea" stroke-width="9" stroke-linecap="round" opacity=".8"/></g><g class="scene-trails"><path d="M390,320 Q596,494 594,647 Q592,800 596,953 Q600,1106 599,1208 Q598,1310 497,1265 Q396,1220 286,1274 Q176,1328 186,1216 Q196,1104 189,997 Q182,890 286,794 Q390,698 283,601 T176,504" fill="none" stroke="#c97e2c" stroke-width="5" stroke-linecap="round" opacity=".45"/><path d="M390,320 Q596,494 594,647 Q592,800 596,953 Q600,1106 599,1208 Q598,1310 497,1265 Q396,1220 286,1274 Q176,1328 186,1216 Q196,1104 189,997 Q182,890 286,794 Q390,698 283,601 T176,504" fill="none" stroke="#a06c24" stroke-width="2.6" stroke-dasharray="2 9" stroke-linecap="round" opacity=".9"/><circle cx="390" cy="320" r="7" fill="#FFFDF6" stroke="#a06c24" stroke-width="2.4"/><circle cx="596" cy="494" r="7" fill="#FFFDF6" stroke="#a06c24" stroke-width="2.4"/><circle cx="592" cy="800" r="7" fill="#FFFDF6" stroke="#a06c24" stroke-width="2.4"/><circle cx="600" cy="1106" r="7" fill="#FFFDF6" stroke="#a06c24" stroke-width="2.4"/><circle cx="598" cy="1310" r="7" fill="#FFFDF6" stroke="#a06c24" stroke-width="2.4"/><circle cx="396" cy="1220" r="7" fill="#FFFDF6" stroke="#a06c24" stroke-width="2.4"/><circle cx="176" cy="1328" r="7" fill="#FFFDF6" stroke="#a06c24" stroke-width="2.4"/><circle cx="196" cy="1104" r="7" fill="#FFFDF6" stroke="#a06c24" stroke-width="2.4"/><circle cx="182" cy="890" r="7" fill="#FFFDF6" stroke="#a06c24" stroke-width="2.4"/><circle cx="390" cy="698" r="7" fill="#FFFDF6" stroke="#a06c24" stroke-width="2.4"/><circle cx="176" cy="504" r="7" fill="#FFFDF6" stroke="#a06c24" stroke-width="2.4"/></g><g class="scene-veg"><g transform="translate(307,632) scale(0.77)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(680,952) scale(1.01)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(427,773) scale(1.03)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(503,1192) scale(0.90)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(149,558) scale(1.03)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(475,475) scale(1.05)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(63,1218) scale(1.08)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(553,535) scale(1.00)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(522,1322) scale(0.72)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(262,990) scale(0.97)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(325,1030) scale(0.92)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(316,440) scale(0.76)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(361,1090) scale(0.89)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(561,917) scale(1.10)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(499,716) scale(0.76)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(86,456) scale(1.09)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(117,1028) scale(0.76)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(565,980) scale(0.89)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(327,834) scale(1.14)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(233,615) scale(1.01)"><g id="tree-sage">
    <ellipse cx="0" cy="1" rx="17" ry="4.5" fill="#2A2620" opacity=".13" filter="url(#soft)"/>
    <circle cx="-7.5" cy="-10" r="11"   fill="#7d9b76"/>
    <circle cx="7"    cy="-8"  r="9"    fill="#93ad8b"/>
    <circle cx="-1"   cy="-19" r="9.5"  fill="#93ad8b"/>
    <circle cx="-2.5" cy="-6"  r="8"    fill="#7d9b76" opacity=".92"/>
  </g></g><g transform="translate(265,1294) scale(0.95)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(667,1143) scale(1.09)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(184,633) scale(0.81)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(76,696) scale(1.03)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(142,776) scale(0.89)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(517,593) scale(1.20)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(461,1110) scale(1.02)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(388,1334) scale(1.16)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(622,516) scale(1.04)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(708,1045) scale(1.01)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(498,644) scale(1.09)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(594,552) scale(1.00)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(459,796) scale(1.07)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(694,1278) scale(0.92)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(471,1212) scale(1.15)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(248,1184) scale(1.08)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(202,925) scale(1.04)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(198,649) scale(0.99)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(82,921) scale(1.18)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g><g transform="translate(625,537) scale(1.13)"><g id="grass-tuft">
    <ellipse cx="0" cy="0" rx="9" ry="2.6" fill="#2A2620" opacity=".10" filter="url(#soft)"/>
    <path d="M-6 0 C -7.5 -5 -5 -8 -4 -10.5 M0 0 C 0 -6 1 -9 2 -12 M6 0 C 7 -4 6 -7 5 -9"
          fill="none" stroke="#7d9b76" stroke-width="2" stroke-linecap="round"/>
    <circle cx="-4.5" cy="-2.5" r="3.4" fill="#93ad8b"/>
    <circle cx="3"    cy="-3"   r="3"   fill="#7d9b76"/>
    <circle cx="0"    cy="-4.5" r="2.4" fill="#93ad8b" opacity=".85"/>
  </g></g></g><g class="scene-dress"><g transform="translate(640,140)"><svg xmlns="http://www.w3.org/2000/svg" viewBox="-40 -145 80 165" width="160" height="330">
  <g transform="translate(0,0)">
    <!-- 气球主体 -->
    <path d="M0,-132 C-22,-132 -28,-112 -28,-98 C-28,-82 -14,-70 0,-68 C14,-70 28,-82 28,-98 C28,-112 22,-132 0,-132 Z"
          fill="#FFFDF6" stroke="#c9b98f" stroke-width="2" stroke-linejoin="round"/>
    <!-- 土色竖纹 3 条 -->
    <path d="M0,-131 Q2,-100 0,-68" fill="none" stroke="#d9cba6" stroke-width="1.5" stroke-linecap="round"/>
    <path d="M-14,-123 Q-12,-96 -7,-70" fill="none" stroke="#d9cba6" stroke-width="1.5" stroke-linecap="round"/>
    <path d="M14,-123 Q12,-96 7,-70" fill="none" stroke="#d9cba6" stroke-width="1.5" stroke-linecap="round"/>
    <!-- 小金星 -->
    <path d="M0,-113.5 L1.6,-109.8 L5.5,-109.4 L2.6,-106.7 L3.4,-102.9 L0,-105 L-3.4,-102.9 L-2.6,-106.7 L-5.5,-109.4 L-1.6,-109.8 Z"
          fill="#e8a84a" stroke="#c97e2c" stroke-width="0.8" stroke-linejoin="round"/>
    <!-- 4 根墨色细绳 -->
    <path d="M-13,-72 L-9,-40" fill="none" stroke="#2A2620" stroke-width="1.5" stroke-linecap="round"/>
    <path d="M-5,-68 L-4,-40" fill="none" stroke="#2A2620" stroke-width="1.5" stroke-linecap="round"/>
    <path d="M5,-68 L4,-40" fill="none" stroke="#2A2620" stroke-width="1.5" stroke-linecap="round"/>
    <path d="M13,-72 L9,-40" fill="none" stroke="#2A2620" stroke-width="1.5" stroke-linecap="round"/>
    <!-- 土色小吊篮 -->
    <rect x="-11" y="-40" width="22" height="15" rx="3" fill="#d9cba6" stroke="#8a6d3b" stroke-width="2"/>
    <path d="M-11,-34 L11,-34" fill="none" stroke="#8a6d3b" stroke-width="1.5" stroke-linecap="round"/>
  </g>
</svg></g><g transform="translate(90,1330)"><g transform="translate(0,0)">
  <ellipse cx="0" cy="0" rx="16" ry="3.5" fill="#c9b98f" opacity="0.55"/>
  <circle cx="0" cy="-26" r="26" fill="none" stroke="#c97e2c" stroke-width="1.5"/>
  <circle cx="0" cy="-26" r="22" fill="#FFFDF6" stroke="#c9b98f" stroke-width="2"/>
  <circle cx="0" cy="-26" r="13" fill="none" stroke="#d9cba6" stroke-width="1.2"/>
  <path d="M16 -26 L20 -26 M11.31 -14.69 L14.14 -11.86 M0 -10 L0 -6 M-11.31 -14.69 L-14.14 -11.86 M-16 -26 L-20 -26 M-11.31 -37.31 L-14.14 -40.14 M0 -42 L0 -46 M11.31 -37.31 L14.14 -40.14" fill="none" stroke="#2A2620" stroke-width="1.6" stroke-linecap="round"/>
  <path d="M0 -40 L3.5 -26 L0 -12 L-3.5 -26 Z" fill="#D9482B"/>
  <circle cx="0" cy="-26" r="2.6" fill="#2A2620"/>
  <circle cx="0" cy="-41.5" r="2" fill="#FFD27A"/>
</g></g><g transform="translate(640,1340)"><g transform="translate(0,0)">
  <ellipse cx="0" cy="2" rx="33" ry="5" fill="#6B6257" opacity="0.18"/>
  <rect x="-32" y="-42" width="64" height="42" rx="7" fill="#d9cba6" stroke="#8a6d3b" stroke-width="2"/>
  <path d="M-30,-39 H30" stroke="#8a6d3b" stroke-width="1.5" opacity="0.45" fill="none"/>
  <path d="M-11,-35 V-6 M11,-35 V-6" stroke="#8a6d3b" stroke-width="1.5" opacity="0.38" fill="none"/>
  <rect x="-6.5" y="-27" width="13" height="15" rx="3" fill="#e8a84a" stroke="#c97e2c" stroke-width="1.5"/>
  <circle cx="0" cy="-20" r="2.2" fill="#a06c24"/>
  <rect x="-30" y="-45.5" width="60" height="3.5" rx="1.7" fill="#FFFDF6" opacity="0.8"/>
  <circle cx="-6" cy="-48" r="1.4" fill="#FFD27A" opacity="0.8"/>
  <circle cx="9" cy="-49" r="1.1" fill="#FFD27A" opacity="0.7"/>
  <g transform="rotate(-25 -28 -40)">
    <rect x="-30" y="-53" width="60" height="13" rx="6" fill="#d9cba6" stroke="#8a6d3b" stroke-width="2"/>
    <rect x="-25" y="-50" width="50" height="7" rx="3.5" fill="#c9b98f"/>
  </g>
</g></g><g transform="translate(530,190)"><svg xmlns="http://www.w3.org/2000/svg" viewBox="-60 -110 120 130" width="120" height="130">
  <g transform="translate(0,0)">
    <ellipse cx="0" cy="-8" rx="27" ry="8" fill="#FFFDF6" opacity="0.5"/>
    <path d="M0 -74 L21 -44 L0 -14 L-21 -44 Z" fill="#a8c6dd" stroke="#2e3a5c" stroke-width="1.5" stroke-linejoin="round"/>
    <path d="M-7 -48 L-1 -66" stroke="#FFFDF6" stroke-width="1.5" stroke-linecap="round" fill="none"/>
    <path d="M4 -52 L7 -58" stroke="#FFFDF6" stroke-width="1.5" stroke-linecap="round" fill="none"/>
    <circle cx="23" cy="-72" r="2.5" fill="#a8c6dd"/>
    <g class="nl" display="none">
      <circle cx="0" cy="-44" r="40" fill="#a8c6dd" opacity="0.4"/>
    </g>
  </g>
</svg></g><g transform="translate(90,700)"><g transform="translate(0,0)">
  <ellipse cx="0" cy="0" rx="9" ry="2.5" fill="#d9cba6" opacity="0.7"/>
  <path d="M0,0 L0,-70" stroke="#2A2620" stroke-width="2.5" stroke-linecap="round"/>
  <circle cx="0" cy="-70" r="2.4" fill="#2A2620"/>
  <path d="M0,-70 Q32,-52 64,-44" fill="none" stroke="#6B6257" stroke-width="1.5" stroke-linecap="round"/>
  <path d="M16,-61.6 L29,-55.7 L22.5,-46 Z" fill="#e8a84a" stroke="#2A2620" stroke-width="1.6" stroke-linejoin="round"/>
  <path d="M32,-54.5 L45,-49.6 L38.5,-40 Z" fill="#D9482B" stroke="#2A2620" stroke-width="1.6" stroke-linejoin="round"/>
  <path d="M48,-48.6 L61,-44.8 L54.5,-35 Z" fill="#a8c6dd" stroke="#2A2620" stroke-width="1.6" stroke-linejoin="round"/>
  <g class="nl" display="none">
    <path d="M54.5,-35 L56,-28" stroke="#6B6257" stroke-width="1.5" stroke-linecap="round"/>
    <circle cx="56" cy="-24" r="8" fill="#FFD27A" opacity="0.28"/>
    <circle cx="56" cy="-24" r="3.4" fill="#FFD27A" stroke="#a06c24" stroke-width="1.4"/>
  </g>
</g></g></g><g class="scene-ff"><g transform="translate(540,520)"><g class="nl" display="none">
    <circle cx="-34" cy="-72" r="10" fill="#FFD27A" opacity=".45"/>
    <circle cx="-34" cy="-72" r="3.2" fill="#FFD27A"/>
    <circle cx="36" cy="-58" r="9" fill="#FFD27A" opacity=".45"/>
    <circle cx="36" cy="-58" r="3" fill="#FFD27A"/>
    <circle cx="26" cy="-96" r="7.5" fill="#FFD27A" opacity=".45"/>
    <circle cx="26" cy="-96" r="2.5" fill="#FFD27A"/>
  </g>
</g><g transform="translate(660,1120)"><g class="nl" display="none">
    <circle cx="-34" cy="-72" r="10" fill="#FFD27A" opacity=".45"/>
    <circle cx="-34" cy="-72" r="3.2" fill="#FFD27A"/>
    <circle cx="36" cy="-58" r="9" fill="#FFD27A" opacity=".45"/>
    <circle cx="36" cy="-58" r="3" fill="#FFD27A"/>
    <circle cx="26" cy="-96" r="7.5" fill="#FFD27A" opacity=".45"/>
    <circle cx="26" cy="-96" r="2.5" fill="#FFD27A"/>
  </g>
</g><g transform="translate(240,900)"><g class="nl" display="none">
    <circle cx="-34" cy="-72" r="10" fill="#FFD27A" opacity=".45"/>
    <circle cx="-34" cy="-72" r="3.2" fill="#FFD27A"/>
    <circle cx="36" cy="-58" r="9" fill="#FFD27A" opacity=".45"/>
    <circle cx="36" cy="-58" r="3" fill="#FFD27A"/>
    <circle cx="26" cy="-96" r="7.5" fill="#FFD27A" opacity=".45"/>
    <circle cx="26" cy="-96" r="2.5" fill="#FFD27A"/>
  </g>
</g></g>
      </svg>
      <div class="night-veil" aria-hidden="true"><svg class="veil-moon moon-d" viewBox="-34 -34 68 68" aria-hidden="true"><path d="M15.5 -26.8 A31 31 0 1 0 15.5 26.8 A28 28 0 1 1 15.5 -26.8 Z" fill="#f7f0cf" stroke="#a06c24" stroke-width="1.6" stroke-linejoin="round" transform="rotate(-18)"/></svg><svg class="veil-moon moon-m" viewBox="-34 -34 68 68" aria-hidden="true"><path d="M15.5 -26.8 A31 31 0 1 0 15.5 26.8 A28 28 0 1 1 15.5 -26.8 Z" fill="#f7f0cf" stroke="#a06c24" stroke-width="1.6" stroke-linejoin="round" transform="rotate(-18)"/></svg></div>

      <div class="hud">
        <span class="brand">Aetrix<b>.</b></span>
        <span class="worldname">🗺 探险世界</span>
        <div class="right">
          <span class="pill" v-if="streak > 0">🔥 连签 {{ streak }} 天</span>
          <button type="button" class="pill amber" v-if="pointsText" @click="goLedger">🪙 {{ pointsText }} 积分</button>
          <span class="pill" v-if="memberInfo"><span class="lv">Lv.{{ memberInfo.level }}</span> {{ memberInfo.level_name }}</span>
          <button type="button" class="avatar" @click="goAccount" aria-label="我的账号">{{ avatarChar }}</button>
        </div>
      </div>

      <div class="m-hud" aria-label="今日状态">
        <div class="m-hello">{{ greetingText }}，{{ nickname }}</div>
        <div class="m-stats">
          <template v-if="streak > 0">连签 <b>{{ streak }}</b> 天 · </template>
          <template v-if="memberInfo"><b>Lv.{{ memberInfo.level }}</b> {{ memberInfo.level_name }}</template>
        </div>
      </div>

      <div class="hello">
        <div class="d">{{ dateLine }}</div>
        <h2>{{ greetingText }}，{{ nickname }}</h2>
        <p v-if="remaining > 0">世界里有 <b>{{ remaining }} 个包裹</b> 等你拆<template v-if="hasLetter">，邮局里有一封昨天的信。</template><template v-else>。</template></p>
        <p v-else-if="hasLetter">邮局里有一封昨天的信，点邮局拆开看看。</p>
        <p v-else>今天片库有更新，去影院看看新到的片子。</p>
      </div>

      <button type="button" class="classic" v-if="variant === 'home'" @click="toClassic">经典视图 ⇄</button>

      <div class="m-zone" aria-hidden="true">🏘 建筑区</div>
      <div class="m-zone-line" aria-hidden="true"></div>
      <div class="m-zone m-zone-2" aria-hidden="true">⛲ 中央广场</div>
      <div class="m-scroll-hint" aria-hidden="true"><span>↓ 上滑继续逛 · 中央广场在下面</span></div>

      <button type="button" class="mark m-cinema" :style="{ left: '28.83%', top: '65.46%' }" @click="openCinema" aria-label="梦幻影院">
        <div class="pin art art-cinema"><svg class="art-svg" viewBox="-78 -164 156 184" fill="none" aria-hidden="true"><g transform="translate(0,0)">
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
        <span class="name">梦幻影院</span>
        <span class="sub">{{ cinemaSub }}</span>
      </button>

      <button type="button" class="mark m-parcels" :style="{ left: '44.64%', top: '68.63%' }" @click="openParcels" :aria-label="remaining > 0 ? `封存驿站，有 ${remaining} 个包裹待拆` : '封存驿站'">
        <span class="dot" v-if="remaining > 0">{{ remaining }}</span>
        <div class="pin art art-archive"><svg class="art-svg" viewBox="-72 -138 144 158" fill="none" aria-hidden="true"><g transform="translate(0,0)">
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
        <span class="name">封存驿站</span>
        <span class="sub">{{ remaining > 0 ? `${remaining} 个包裹待拆` : '今天的包裹都拆完啦' }}</span>
      </button>

      <button type="button" class="mark lighthouse m-lighthouse" :style="{ left: '82.3%', top: '62.5%' }" @click="goServer" aria-label="传送灯塔">
        <div class="pin art art-lighthouse"><svg class="art-svg" viewBox="-52 -238 104 258" fill="none" aria-hidden="true"><g transform="translate(0,0)">
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
        <span class="name">传送灯塔</span>
        <span class="sub">{{ lighthouseSub }}</span>
      </button>

      <button type="button" class="mark m-shop" :style="{ left: '36.10%', top: '83.14%' }" @click="goShop" aria-label="补给站">
        <div class="pin art art-supply"><svg class="art-svg" viewBox="-56 -106 112 126" fill="none" aria-hidden="true"><g transform="translate(0,0)">
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
        <span class="name">补给站</span>
        <span class="sub">商店 · 充积分买会员</span>
      </button>

      <button type="button" class="mark m-collector" :style="{ left: '12.50%', top: '68.63%' }" @click="goCollector" aria-label="收藏家小屋">
        <div class="pin art art-collector"><svg class="art-svg" viewBox="-60 -103 138 122" fill="none" aria-hidden="true"><g transform="translate(0,0)">
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
        <span class="name">收藏家小屋</span>
        <span class="sub">{{ collectorSub }}</span>
      </button>

      <button type="button" class="mark m-post" :style="{ left: '17.35%', top: '47.54%' }" @click="openLetter" :aria-label="hasLetter && !letterRead ? '邮局，有一封未读来信' : '邮局'">
        <span class="dot" v-if="hasLetter && !letterRead">1</span>
        <div class="pin art art-post"><svg class="art-svg" viewBox="-60 -124 120 144" fill="none" aria-hidden="true"><g transform="translate(0,0)">
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
        <span class="name">邮局</span>
        <span class="sub">{{ postSub }}</span>
      </button>

      <button type="button" class="mark m-gamehall" :style="{ left: '56.12%', top: '82.24%' }" @click="goGame" aria-label="游戏大厅">
        <div class="pin art art-hall"><svg class="art-svg" viewBox="-60 -133 120 153" fill="none" aria-hidden="true"><g transform="translate(0,0)">
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
        <span class="name">游戏大厅</span>
        <span class="sub">签到 · 许愿池 · 追新历</span>
      </button>

      <button type="button" class="mark m-stele" :style="{ left: '51.40%', top: '41.42%' }" @click="openStele" aria-label="观影碑">
        <div class="pin art art-stele"><svg class="art-svg" viewBox="-48 -204 96 222" fill="none" aria-hidden="true"><g transform="translate(0,0)">
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
        <span class="name">观影碑</span>
        <span class="sub">{{ steleSub }}</span>
      </button>

      <button type="button" class="mark m-board" :style="{ left: '22%', top: '82.5%' }" @click="openBoard" aria-label="服主告示牌">
        <div class="pin art art-board"><svg class="art-svg" viewBox="-52 -108 104 128" fill="none" aria-hidden="true"><g transform="translate(0,0)">
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
        <span class="name">告示牌</span>
        <span class="sub">{{ boardSub }}</span>
      </button>

      <button type="button" class="mark m-wall" :style="{ left: '76.8%', top: '44%' }" @click="openWall" aria-label="到此一游留言墙">
        <div class="pin art art-walltree"><svg class="art-svg" viewBox="-76 -170 148 190" fill="none" aria-hidden="true"><g transform="translate(0,0)">
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
        <span class="name">留言墙</span>
        <span class="sub">{{ wallSub }}</span>
      </button>

      <button type="button" class="mark m-fortune" :class="{ 'fortune-pending': !fortuneDrew }" :style="{ left: '67.09%', top: '66.36%' }" @click="openFortune" aria-label="每日抽一签">
        <div class="pin art art-fortune"><svg class="art-svg" viewBox="-70 -168 140 188" fill="none" aria-hidden="true"><g transform="translate(0,0)">
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
        <span class="name">抽签亭</span>
        <span class="sub">{{ fortuneSub }}</span>
      </button>

      <div class="player m-player" :style="{ left: '49%', top: '88%' }">
        <div class="tok">{{ avatarChar }}</div>
        <span class="who">你在这里</span>
      </div>

      <div class="m-feed" v-if="feedItems.length > 0" aria-hidden="true"><i>●</i> {{ feedItems[0].time }} {{ feedItems[0].text }}</div>

      <div class="feed">
        <h4><i>●</i> 世界动态 · 服务器在忙什么</h4>
        <ul>
          <li v-if="feedState === 'loading'">正在接收世界动态…</li>
          <li v-else-if="feedState === 'error'">动态没加载出来，点上面的建筑照样能玩</li>
          <template v-else>
            <li v-for="(it, idx) in feedItems" :key="idx"><b v-if="it.time">{{ it.time }}</b> {{ it.text }}</li>
            <li v-if="feedItems.length === 0">暂无动态</li>
          </template>
        </ul>
      </div>

      <div class="quest">
        <h4>📋 今日任务</h4>
        <span class="stamp-done" v-if="questPercent >= 100">今日探险完成 ✓</span>
        <button type="button" class="q" :class="{ done: q.done }" v-for="q in quests" :key="q.key" @click="onQuest(q.key)">
          <span class="box">{{ q.done ? '✓' : '' }}</span>{{ q.label }}<span class="rw" v-if="q.rw">{{ q.rw }}</span>
        </button>
        <div class="bar"><i :style="{ width: questPercent + '%' }"></i></div>
      </div>
    </div>

    <div class="w-overlay" v-if="overlay !== 'none'" @click.self="closeOverlay">
      <div class="w-sheet" :class="{ 'is-stele': overlay === 'stele' }" role="dialog" aria-modal="true" tabindex="-1" ref="sheetEl"
           @touchstart="onSheetTouchStart" @touchend="onSheetTouchEnd">
        <div class="w-grip" aria-hidden="true"></div>
        <div class="w-sheet-head" v-if="overlay !== 'stele'">
          <span class="w-sheet-title">{{ sheetTitle }}</span>
          <button type="button" class="w-sheet-close" @click="closeOverlay" aria-label="关闭">✕</button>
        </div>
        <template v-if="overlay === 'parcels'">
          <p class="w-sheet-empty" v-if="parcelsLoading && parcelList.length === 0">正在搬包裹…</p>
          <p class="w-sheet-empty" v-else-if="parcelList.length === 0">今天还没有新封存，明天再来拆 📦</p>
          <div class="w-parcel-grid" v-else>
            <ParcelBox
              v-for="p in parcelList"
              :key="String(p.id)"
              :parcel="p"
              :busy="unsealingIds.has(String(p.id))"
              @unseal="onUnseal"
            />
          </div>
        </template>
        <template v-else-if="overlay === 'letter'">
          <WaxLetter :letter="letterData" />
        </template>
        <template v-else-if="overlay === 'stele'">
          <div class="lb-head">
            <div class="lb-arch"><svg width="150" height="30" viewBox="0 0 150 30" fill="none" aria-hidden="true"><path d="M6 27 Q75 -14 144 27" stroke="#c97e2c" stroke-width="1.6"/><path d="M24 27 Q75 2 126 27" stroke="#d9cba6" stroke-width="1.3"/><circle cx="75" cy="7" r="10.5" fill="#FCF8EC"/><path d="M75 3 l2.5 5.4 5.9.7 -4.4 4 1.2 5.8 -5.2-3 -5.2 3 1.2-5.8 -4.4-4 5.9-.7 z" fill="#e8a84a" stroke="#c97e2c" stroke-width="1.1" stroke-linejoin="round"/></svg></div>
            <div class="lb-title">观影碑</div>
            <div class="lb-sub">排行榜 · <b>{{ lbPeriod === 'day' ? '今日' : '本周' }}</b> {{ lbPeriod === 'day' ? '00:00 起' : '周一 00:00 起' }}</div>
            <button type="button" class="w-sheet-close lb-close" @click="closeOverlay" aria-label="关闭">✕</button>
          </div>
          <div class="lb-tabs">
            <button type="button" class="lb-tab" :class="{ on: lbBoard === 'hot' }" @click="lbBoard = 'hot'"><svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true"><circle cx="8" cy="8" r="6.6" stroke="currentColor" stroke-width="1.5"/><path d="M6.4 5.2 L11 8 L6.4 10.8 Z" fill="currentColor"/></svg>热播榜</button>
            <button type="button" class="lb-tab" :class="{ on: lbBoard === 'duration' }" @click="lbBoard = 'duration'"><svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M4.5 2.5 h7 M4.5 13.5 h7 M5.5 2.5 c0 3.4 5 3.4 5 5.5 c0 2.1 -5 2.1 -5 5.5 M10.5 2.5 c0 3.4 -5 3.4 -5 5.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>观影时长榜</button>
          </div>
          <div class="lb-periods">
            <span class="lb-plabel">碑刻周期</span>
            <button type="button" class="lb-period" :class="{ on: lbPeriod === 'day' }" @click="lbPeriod = 'day'">日榜</button>
            <button type="button" class="lb-period" :class="{ on: lbPeriod === 'week' }" @click="lbPeriod = 'week'">周榜</button>
          </div>
          <p class="w-sheet-empty" v-if="lbLoading && lbRows.length === 0">正在抄榜…</p>
          <p class="w-sheet-empty" v-else-if="lbRows.length === 0">还没有人上榜，快去看片抢头名 🏆</p>
          <template v-else>
            <div class="lb-podium">
              <div v-for="slot in lbPodium" :key="slot.rank" class="lb-pod" :class="`r${slot.rank}`">
                <div class="lb-medal"><svg class="lb-crown" v-if="slot.rank === 1" width="26" height="16" viewBox="0 0 26 16" fill="none" aria-hidden="true"><path d="M2 14 L4 4 L9.5 9 L13 2.5 L16.5 9 L22 4 L24 14 Z" fill="#e8a84a" stroke="#c97e2c" stroke-width="1.3" stroke-linejoin="round"/></svg>{{ slot.rank }}<span class="lb-sealdot" v-if="slot.rank === 1">冠</span></div>
                <div class="lb-pname">{{ slot.title }}</div>
                <div class="lb-ptype" v-if="lbBoard === 'hot'">{{ slot.type }}</div>
                <div class="lb-pnum">{{ slot.num }} <small>{{ slot.unit }}</small></div>
                <div class="lb-plinth"></div>
              </div>
            </div>
            <div class="lb-divider"><i></i></div>
            <ul class="lb-rows">
              <li v-for="(r, i) in lbRest" :key="String(r.item_id ?? r.user_id ?? i)" class="lb-row" :class="{ me: lbIsMe(r) }">
                <span class="lb-rk">{{ i + 4 }}</span>
                <span class="lb-rname">{{ lbTitle(r) }}</span>
                <span class="lb-tag" v-if="lbBoard === 'hot'">{{ String(r.item_type ?? '') }}</span>
                <span class="lb-metag" v-if="lbIsMe(r)">我</span>
                <span class="lb-rnum">{{ lbNumText(r) }} <small>{{ lbUnitText() }}</small></span>
              </li>
            </ul>
          </template>
          <div class="lb-myrank">
            <div class="lb-stamp"><b>{{ lbStampTop }}</b><span>{{ lbStampBottom }}</span></div>
            <div class="lb-mtxt">{{ lbMyLead }}<b v-if="lbMyStrong">{{ lbMyStrong }}</b><template v-if="lbMyGap">，距上一名只差 <span class="gap">{{ lbMyGap }}</span></template>{{ lbMyTail }}</div>
          </div>
          <p class="lb-scope">{{ lbScopeLine }}</p>
        </template>
        <template v-else-if="overlay === 'board'">
          <div v-if="boardData.visible === true" class="w-board">
            <p class="w-board-text">{{ String(boardData.text ?? '') }}</p>
            <p class="w-board-sig">—— {{ String(boardData.signature ?? '服主') }}</p>
          </div>
          <p class="w-sheet-empty" v-else>服主还没留言，改天再来看看 📢</p>
        </template>
        <template v-else-if="overlay === 'wall'">
          <p class="w-sheet-empty" v-if="wallLoading && wallPosts.length === 0">正在翻墙…</p>
          <ul class="w-wall-list" v-else-if="wallPosts.length > 0">
            <li v-for="p in wallPosts" :key="String(p.id)" class="w-wall-row">
              <span class="w-wall-nick">{{ String(p.nickname ?? '') }}</span>
              <span class="w-wall-time">{{ fmtWallTime(p.created_at) }}</span>
              <p class="w-wall-text">{{ String(p.content ?? '') }}</p>
            </li>
          </ul>
          <p class="w-sheet-empty" v-else>墙上还空着，来留下第一句话吧 💬</p>
          <div class="w-wall-form">
            <input v-model="wallInput" maxlength="60" placeholder="到此一游，留句话吧（60字内）" @keyup.enter="submitWall" aria-label="留言内容" />
            <button type="button" class="w-wall-btn" :disabled="wallPosting || !wallInput.trim()" @click="submitWall">
              {{ wallPosting ? '贴上…' : '贴上墙' }}
            </button>
          </div>
          <p class="w-sheet-foot">每人每 10 分钟可留 1 条</p>
        </template>
        <template v-else-if="overlay === 'fortune'">
          <div v-if="fortuneLoading && !fortune" class="w-sheet-empty">正在摇签筒…</div>
          <div v-else-if="fortuneDrew" class="w-fortune">
            <div class="w-fortune-level">{{ String(fortune?.level ?? '') }}</div>
            <p class="w-fortune-text">{{ String(fortune?.text ?? '') }}</p>
            <p class="w-fortune-yiji"><b>宜</b> {{ String(fortune?.yi ?? '') }}　<b>忌</b> {{ String(fortune?.ji ?? '') }}</p>
            <p class="w-sheet-foot">每天 1 次 · 0 点刷新</p>
          </div>
          <div v-else class="w-fortune-draw">
            <div class="w-fortune-pot">🎋</div>
            <p class="w-sheet-empty">心诚则灵，点一下抽今日运势</p>
            <button type="button" class="w-fortune-btn" :disabled="fortuneDrawing" @click="drawFortune">
              {{ fortuneDrawing ? '摇签中…' : '抽一签' }}
            </button>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useRouter } from 'vue-router'
import { usePanelStore } from '@/stores/panel'
import { useUserStore } from '@/stores/user'
import { humanizeError, panelApi } from '@/composables/usePanelApi'
import type { ParcelItem, ParcelsData } from '@/composables/usePanelApi'
import { checkinApi, memberApi } from '@/api/economy'
import { useHomeViewMode } from '@/composables/useHomeViewMode'
import { ensureEmbyBase } from '@/api/emby'
import { useToast } from '@/composables/useToast'
import ParcelBox from '@/components/panel/ParcelBox.vue'
import WaxLetter from '@/components/panel/WaxLetter.vue'

/** 世界地图本体：首页（variant=home）与探险日志 /game（variant=game）共用同一份数据与弹层 */
withDefaults(defineProps<{ variant?: 'home' | 'game' }>(), { variant: 'home' })

const router = useRouter()
const panel = usePanelStore()
const user = useUserStore()
const { setMode } = useHomeViewMode()

const { today, parcels, pulse, letter, board, collector } = storeToRefs(panel)

/* ---------- 本地状态 ---------- */
type CheckinStatus = {
  enabled?: boolean
  checked_today?: boolean
  streak?: number
  base_points?: number
  streak_bonus?: number
  streak_max_bonus?: number
  points?: number
}

const checkin = ref<CheckinStatus | null>(null)
const memberRaw = ref<Record<string, unknown> | null>(null)

const overlay = ref<'none' | 'parcels' | 'letter' | 'stele' | 'board' | 'wall' | 'fortune'>('none')
const unsealingIds = ref(new Set<string>())
const sheetEl = ref<HTMLElement | null>(null)
let lastTrigger: HTMLElement | null = null

/* ---------- 基础数据 ---------- */
const todayD = computed<Record<string, unknown>>(
  () => (today.value.data ?? {}) as Record<string, unknown>,
)

const userRaw = computed(
  () => (user.user ?? null) as unknown as Record<string, unknown> | null,
)

const userId = computed(() => {
  const id = userRaw.value?.id
  return id === undefined || id === null || id === '' ? 'guest' : String(id)
})

const nickname = computed(() => {
  const n = todayD.value.nickname
  if (typeof n === 'string' && n.trim()) return n
  const u = userRaw.value
  const un = u?.nickname
  if (typeof un === 'string' && un.trim()) return un
  const name = u?.username
  if (typeof name === 'string' && name.trim()) return name
  return '探险家'
})

const avatarChar = computed(() => Array.from(nickname.value)[0] ?? '探')

const greetingText = computed(() => {
  const g = todayD.value.greeting
  if (typeof g === 'string' && g.trim()) return g
  const h = new Date(nowTick.value).getHours()
  if (h < 5) return '夜深了'
  if (h < 9) return '早上好'
  if (h < 12) return '上午好'
  if (h < 14) return '中午好'
  if (h < 18) return '下午好'
  return '晚上好'
})

const dateLine = computed(() => {
  const raw = todayD.value.date_label
  let m: number
  let d: number
  if (typeof raw === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(raw)) {
    // 手动拆分，避免 new Date 解析带来的时区偏移
    const parts = raw.split('-')
    m = Number(parts[1])
    d = Number(parts[2])
  } else {
    const now = new Date()
    m = now.getMonth() + 1
    d = now.getDate()
  }
  let wd = todayD.value.weekday
  if (typeof wd !== 'string' || !wd) {
    wd = ['日', '一', '二', '三', '四', '五', '六'][new Date().getDay()] ?? ''
  }
  return `探险日志 · ${m} 月 ${d} 日 星期${wd}`
})

/* ---------- 包裹 ---------- */
const parcelList = computed<ParcelItem[]>(() => {
  const d = parcels.value.data
  if (!d || typeof d !== 'object' || Array.isArray(d)) return []
  const arr = (d as ParcelsData).parcels
  return Array.isArray(arr) ? (arr as ParcelItem[]) : []
})

const parcelsLoading = computed(() => parcels.value.loading === true)

const parcelCount = computed(() => {
  const n = todayD.value.parcel_count
  if (typeof n === 'number') return n
  return parcelList.value.length
})

const unsealedCount = computed(() => {
  const n = todayD.value.unsealed_count
  if (typeof n === 'number') return n
  return parcelList.value.filter((p) => p.unsealed).length
})

const remaining = computed(() => parcelList.value.filter((p) => !p.unsealed).length)

/* ---------- 信件 ---------- */
const letterData = computed<Record<string, unknown>>(() => {
  const d = letter.value.data
  if (d && typeof d === 'object' && !Array.isArray(d)) {
    return d as Record<string, unknown>
  }
  return {}
})

const hasLetter = computed(() => Object.keys(letterData.value).length > 0)

const letterDateKey = computed(() => {
  const d = letterData.value.date
  if (typeof d === 'string' && d) return d
  const now = new Date()
  const mm = String(now.getMonth() + 1).padStart(2, '0')
  const dd = String(now.getDate()).padStart(2, '0')
  return `${now.getFullYear()}-${mm}-${dd}`
})

const letterReadKey = computed(
  () => `aetrix:world-letter-read:u${userId.value}:${letterDateKey.value}`,
)

const letterReadState = ref(false)

function refreshLetterRead() {
  try {
    letterReadState.value = localStorage.getItem(letterReadKey.value) === '1'
  } catch {
    letterReadState.value = false
  }
}

const letterRead = computed(() => letterReadState.value)

function markLetterRead() {
  letterReadState.value = true
  try {
    localStorage.setItem(letterReadKey.value, '1')
  } catch {
    /* 静默失败 */
  }
}

/* ---------- 积分 / 会员 / 副标题 ---------- */
const pointsText = computed<string | null>(() => {
  const c = checkin.value?.points
  if (typeof c === 'number') return Math.trunc(c).toLocaleString('en-US')
  const d = collector.value.data
  if (d && typeof d === 'object' && !Array.isArray(d)) {
    const p = (d as Record<string, unknown>).points
    if (typeof p === 'number') return Math.trunc(p).toLocaleString('en-US')
  }
  return null
})

const memberInfo = computed<{ level: number; level_name: string } | null>(() => {
  const m = memberRaw.value
  if (!m) return null
  const lvl = m.level
  if (typeof lvl !== 'number') return null
  return { level: lvl, level_name: String(m.level_name || '探险家') }
})

const streak = computed(() => {
  const s = todayD.value.streak_days
  if (typeof s === 'number') return s
  const c = checkin.value?.streak
  if (typeof c === 'number') return c
  const d = collector.value.data
  if (d && typeof d === 'object' && !Array.isArray(d)) {
    const cs = (d as Record<string, unknown>).streak
    if (typeof cs === 'number') return cs
  }
  return 0
})

const cinemaSub = computed(() => {
  if (parcelCount.value <= 0) return '片库 · 随时开场'
  let base = `片库 · 今日新到 ${parcelCount.value} 部`
  const first = parcelList.value[0]
  const raw = String(first?.title || first?.name || '').trim()
  if (raw) {
    const name = raw.length > 7 ? `${raw.slice(0, 7)}…` : raw
    base += `，含《${name}》`
  }
  return base
})

const postSub = computed(() => {
  if (!hasLetter.value) return '暂无新信'
  if (letterRead.value) return '昨天的信到了'
  const teaser = typeof letterData.value.teaser === 'string' ? letterData.value.teaser.trim() : ''
  if (teaser) {
    const t = teaser.length > 12 ? `${teaser.slice(0, 12)}…` : teaser
    return `信里说：『${t}』`
  }
  const rec = typeof letterData.value.recommendation === 'string' ? letterData.value.recommendation.trim() : ''
  if (rec) {
    const r = rec.length > 12 ? `${rec.slice(0, 12)}…` : rec
    return `今日推荐：『${r}』`
  }
  return '昨天的信到了'
})

const lighthouseSub = computed(() => {
  const d = pulse.value.data
  if (!d || typeof d !== 'object' || Array.isArray(d)) return '服务器'
  const health = (d as Record<string, unknown>).health
  if (health && typeof health === 'object' && !Array.isArray(health)) {
    const n = (health as Record<string, unknown>).issues_count
    if (typeof n === 'number' && n > 0) return `服务器 · 有 ${n} 件事在处理`
  }
  return '服务器 · 一切正常'
})

const collectorSub = computed(() => {
  const d = collector.value.data
  if (d && typeof d === 'object' && !Array.isArray(d)) {
    const rec = d as Record<string, unknown>
    const list = rec.achievements
    if (Array.isArray(list) && list.length > 0) {
      const lit = list.filter(
        (a) => a && typeof a === 'object' && (a as Record<string, unknown>).unlocked === true,
      ).length
      if (list.length > lit) {
        if (list.length - lit === 1) return `徽章墙 · ${lit}/${list.length} 枚 · 还差 1 枚`
        return `徽章墙 · 已点亮 ${lit}/${list.length} 枚`
      }
    }
    const n = rec.achievement_count
    if (typeof n === 'number') return `徽章墙 · 已点亮 ${n} 枚`
  }
  return '徽章墙'
})

/* ---------- 动态 ---------- */
function cleanActivityText(t: unknown): string {
  if (typeof t !== 'string') return '服务器刚才忙去了，这条动态没写清楚'
  const s = t.trim()
  if (s.startsWith('{')) {
    if (/['"]message['"]/.test(s)) {
      const m1 = s.match(/'message'\s*:\s*'((?:[^'\\]|\\.)*)'/)
      if (m1) return m1[1].replace(/\\'/g, "'")
      const m2 = s.match(/"message"\s*:\s*"((?:[^"\\]|\\.)*)"/)
      if (m2) return m2[1].replace(/\\"/g, '"')
    }
    return '服务器刚才忙去了，这条动态没写清楚'
  }
  return s
}

const feedItems = computed<Array<{ time: string; text: string }>>(() => {
  const d = pulse.value.data
  if (!d || typeof d !== 'object' || Array.isArray(d)) return []
  const acts = (d as Record<string, unknown>).activities
  if (!Array.isArray(acts)) return []
  const out: Array<{ time: string; text: string }> = []
  for (const a of acts.slice(0, 3)) {
    const rec = (a ?? {}) as Record<string, unknown>
    const text = cleanActivityText(rec.text)
    if (!text) continue
    let time = ''
    const at = rec.at
    if (typeof at === 'string' || typeof at === 'number') {
      const dt = new Date(at)
      if (!Number.isNaN(dt.getTime())) {
        time = `${String(dt.getHours()).padStart(2, '0')}:${String(dt.getMinutes()).padStart(2, '0')}`
      }
    }
    out.push({ time, text })
  }
  return out
})

const feedState = computed<'loading' | 'error' | 'ok'>(() => {
  if (pulse.value.error) return 'error'
  if (pulse.value.loading && !pulse.value.data) return 'loading'
  return 'ok'
})

/* ---------- 任务 ---------- */
const quests = computed<Array<{ key: string; label: string; done: boolean; rw: string }>>(() => {
  const ck = checkin.value
  const checkinDone = ck?.checked_today === true
  const bp = ck?.base_points
  const parcelsDone = parcelCount.value > 0 && unsealedCount.value >= 1
  const read = letterRead.value
  return [
    {
      key: 'checkin',
      label: '每日签到',
      done: checkinDone,
      rw: checkinDone ? '已完成' : typeof bp === 'number' && bp > 0 ? `+${bp} 积分` : '',
    },
    {
      key: 'parcels',
      label: '拆一个封存包裹',
      done: parcelsDone,
      rw: parcelCount.value > 0 ? `${unsealedCount.value}/${parcelCount.value}` : '今日无新包裹',
    },
    {
      key: 'letter',
      label: '读昨天的信',
      done: read,
      rw: !hasLetter.value ? '暂无信件' : read ? '已读' : '0/1',
    },
  ]
})

const questPercent = computed(() => {
  const done = quests.value.filter((q) => q.done).length
  return (done / 3) * 100
})

const questDoneCount = computed(() => quests.value.filter((q) => q.done).length)
const questNextLabel = computed(() => quests.value.find((q) => !q.done)?.label ?? '')

/* ---------- 弹层 ---------- */
function openParcels() {
  lastTrigger = document.activeElement as HTMLElement | null
  overlay.value = 'parcels'
  nextTick(() => {
    try { sheetEl.value?.focus() } catch { /* 静默 */ }
  })
}

function openLetter() {
  if (!hasLetter.value) return
  markLetterRead()
  lastTrigger = document.activeElement as HTMLElement | null
  overlay.value = 'letter'
  nextTick(() => {
    try { sheetEl.value?.focus() } catch { /* 静默 */ }
  })
}

function closeOverlay() {
  overlay.value = 'none'
  nextTick(() => {
    if (lastTrigger && document.contains(lastTrigger)) {
      lastTrigger.focus()
    }
    lastTrigger = null
  })
}

/* 抽屉下滑关闭（仅手机端：无 touch 的桌面端不受影响） */
const sheetTouchY = ref<number | null>(null)
function onSheetTouchStart(e: TouchEvent) {
  if (e.touches.length === 1) sheetTouchY.value = e.touches[0].clientY
}
function onSheetTouchEnd(e: TouchEvent) {
  if (sheetTouchY.value === null) return
  const dy = e.changedTouches[0].clientY - sheetTouchY.value
  sheetTouchY.value = null
  if (dy > 90 && window.matchMedia('(max-width: 1023px)').matches) closeOverlay()
}

function openOverlaySheet(kind: 'stele' | 'board' | 'wall' | 'fortune') {
  lastTrigger = document.activeElement as HTMLElement | null
  overlay.value = kind
  nextTick(() => {
    try { sheetEl.value?.focus() } catch { /* 静默 */ }
  })
}

/* ---------- 观影碑 · 排行榜 ---------- */
const lbBoard = ref<'hot' | 'duration'>('hot')
const lbPeriod = ref<'day' | 'week'>('day')
const lbData = ref<{ items: Array<Record<string, unknown>>; my_rank: number | null } | null>(null)
const lbLoading = ref(false)

async function loadLeaderboard() {
  lbLoading.value = true
  try {
    const d = await panelApi.leaderboard(lbBoard.value, lbPeriod.value)
    lbData.value = { items: d.items ?? [], my_rank: d.my_rank ?? null }
  } catch {
    lbData.value = null
  } finally {
    lbLoading.value = false
  }
}

/* 观影碑副标题独立数据源：固定口径「今日热播第一名」，不受排行榜弹层浏览状态影响 */
const steleTop = ref<Record<string, unknown> | null>(null)
const steleLoaded = ref(false)

async function loadSteleTop() {
  try {
    const res = await panelApi.leaderboard('hot', 'day')
    const items = ((res as { items?: Record<string, unknown>[] } | null)?.items ?? []) as Record<string, unknown>[]
    steleTop.value = items.length ? items[0] : null
    steleLoaded.value = true
  } catch {
    /* 静默失败：不影响地图其他加载；loaded 保持 false，副标题回落中性标签 */
    steleTop.value = null
  }
}

function openStele() {
  void loadLeaderboard()
  openOverlaySheet('stele')
}

watch([lbBoard, lbPeriod], () => { void loadLeaderboard() })

const lbRows = computed(() => lbData.value?.items ?? [])
const lbMyRank = computed(() => lbData.value?.my_rank ?? null)
const lbScopeText = computed(() =>
  lbPeriod.value === 'day' ? '今日（自然日）· 全服' : '本周（自然周）· 全服',
)

/* 排行榜新版式（碑座 + 刻字行 + 我的排名印章条） */
function lbTitle(r: Record<string, unknown>): string {
  return lbBoard.value === 'hot' ? String(r.name ?? '') : String(r.nickname ?? '')
}
function lbNumText(r: Record<string, unknown>): string {
  return lbBoard.value === 'hot' ? String(Number(r.count ?? 0)) : fmtHours(r.seconds).replace(/\s*小时$/, '')
}
function lbUnitText(): string {
  return lbBoard.value === 'hot' ? '次' : '小时'
}
function lbIsMe(r: Record<string, unknown>): boolean {
  return lbBoard.value === 'duration' && String(r.nickname ?? '') === nickname.value
}
const lbPodium = computed(() => {
  const top = lbRows.value.slice(0, 3).map((r, i) => ({
    rank: i + 1,
    title: lbTitle(r),
    type: String(r.item_type ?? ''),
    num: lbNumText(r),
    unit: lbBoard.value === 'hot' ? (i === 0 && lbBoard.value === 'hot' ? '次播放' : '次') : '小时',
  }))
  /* 视觉顺序：亚军左、冠军中、季军右 */
  const order = [top[1], top[0], top[2]]
  return order.filter((s) => s !== undefined)
})
const lbRest = computed(() => lbRows.value.slice(3))
const lbMyRow = computed(() =>
  lbBoard.value === 'duration' ? lbRows.value.find((r) => lbIsMe(r)) ?? null : null,
)
const lbMyGap = computed<string | null>(() => {
  const rank = lbMyRank.value
  if (lbBoard.value !== 'duration' || !rank || rank <= 1 || !lbMyRow.value) return null
  const prev = lbRows.value[rank - 2]
  if (!prev) return null
  const gapSec = Number(prev.seconds ?? 0) - Number(lbMyRow.value.seconds ?? 0)
  if (!Number.isFinite(gapSec) || gapSec <= 0) return null
  const g = fmtHours(gapSec)
  return g === '0 小时' ? null : g
})
const lbStampTop = computed(() =>
  lbBoard.value === 'duration' && lbMyRank.value ? String(lbMyRank.value) : '榜',
)
const lbStampBottom = computed(() =>
  lbBoard.value === 'duration' && lbMyRank.value ? '名' : '外',
)
const lbMyLead = computed(() => {
  if (lbBoard.value === 'hot') return '热播榜是全服的榜，你的口味在'
  if (!lbMyRank.value) return '你还没上榜——去看一部片子，把名字刻上碑。'
  return `你${lbPeriod.value === 'day' ? '今天' : '本周'}排在`
})
const lbMyStrong = computed(() => {
  if (lbBoard.value === 'hot') return '时长榜'
  if (!lbMyRank.value) return ''
  return `第 ${lbMyRank.value} 名`
})
const lbMyTail = computed(() => {
  if (lbBoard.value === 'hot') return '见分晓——去看看你排第几。'
  if (!lbMyRank.value) return ''
  if (lbMyRank.value === 1) return '，碑顶就是你，守住它。'
  return lbMyGap.value ? '——再看一部就追上了。' : '。'
})
const lbScopeLine = computed(() =>
  lbBoard.value === 'hot'
    ? `${lbScopeText.value} · 按全服播放次数统计`
    : `${lbScopeText.value} · 时长榜只显示昵称，可在「我的账号 → 设置」里关掉自己上榜`,
)

const steleSub = computed(() => {
  if (!steleLoaded.value) return '观影碑'
  const first = steleTop.value
  if (!first) return '虚位以待，等你开播'
  const name = String(first.name ?? first.nickname ?? '')
  const count = Number(first.count ?? 0)
  return `今日最热 · ${name} ${count}次`
})

/* ---------- 服主告示牌 ---------- */
const boardData = computed<Record<string, unknown>>(
  () => (board.value.data ?? {}) as Record<string, unknown>,
)

const boardSub = computed(() => {
  if (boardData.value.visible === true) {
    const sig = String(boardData.value.signature ?? '服主')
    const text = typeof boardData.value.text === 'string' ? boardData.value.text.trim() : ''
    if (!text) return `服主留言 · ${sig}`
    const hook = text.length <= 12 ? text : `${text.slice(0, 12)}…`
    return `『${hook}』——${sig}`
  }
  return '等服主留言'
})

function openBoard() {
  openOverlaySheet('board')
}

/* ---------- 到此一游 · 留言墙 ---------- */
const wallPosts = ref<Array<Record<string, unknown>>>([])
const wallLoading = ref(false)
const wallInput = ref('')
const wallPosting = ref(false)

async function loadWall() {
  wallLoading.value = true
  try {
    const d = await panelApi.wallList()
    wallPosts.value = Array.isArray(d.posts) ? d.posts : []
  } catch {
    wallPosts.value = []
  } finally {
    wallLoading.value = false
  }
}

async function submitWall() {
  const c = wallInput.value.trim()
  if (!c || wallPosting.value) return
  if (c.length > 60) {
    useToast().warning('留言最多 60 字')
    return
  }
  wallPosting.value = true
  try {
    await panelApi.wallPost(c)
    wallInput.value = ''
    await loadWall()
  } catch (e) {
    useToast().error(humanizeError(e))
  } finally {
    wallPosting.value = false
  }
}

function parseWallTime(v: unknown): number {
  if (v === null || v === undefined || v === '') return NaN
  if (typeof v === 'number') return v < 1e12 ? v * 1000 : v
  if (typeof v === 'string') {
    const t = Date.parse(v)
    return Number.isNaN(t) ? NaN : t
  }
  return NaN
}

const wallSub = computed(() => {
  const posts = wallPosts.value
  if (posts.length === 0) return '来留下第一句话吧'
  let sub = `已有 ${posts.length} 条足迹`
  let latest = -Infinity
  for (const p of posts) {
    const t = parseWallTime(p?.created_at)
    if (!Number.isNaN(t) && t > latest) latest = t
  }
  if (latest !== -Infinity) {
    const diff = Date.now() - latest
    if (diff >= 0) {
      const MIN = 60 * 1000
      const HOUR = 60 * MIN
      if (diff < HOUR) sub += ` · 最新 ${Math.max(1, Math.floor(diff / MIN))} 分钟前`
      else if (diff < 24 * HOUR) sub += ` · 最新 ${Math.floor(diff / HOUR)} 小时前`
    }
  }
  return sub
})

function openWall() {
  void loadWall()
  openOverlaySheet('wall')
}

/* ---------- 每日抽一签 ---------- */
const fortune = ref<Record<string, unknown> | null>(null)
const fortuneLoading = ref(false)
const fortuneDrawing = ref(false)

async function loadFortune() {
  fortuneLoading.value = true
  try {
    fortune.value = await panelApi.fortuneToday()
  } catch {
    fortune.value = null
  } finally {
    fortuneLoading.value = false
  }
}

async function drawFortune() {
  if (fortuneDrawing.value) return
  fortuneDrawing.value = true
  try {
    fortune.value = await panelApi.fortuneDraw()
  } catch (e) {
    useToast().error(humanizeError(e))
  } finally {
    fortuneDrawing.value = false
  }
}

const fortuneDrew = computed(() => fortune.value?.drew === true)

const fortuneSub = computed(() =>
  fortuneDrew.value ? `今日：${String(fortune.value?.level ?? '')}` : '每日一签，试试手气',
)

function openFortune() {
  void loadFortune()
  openOverlaySheet('fortune')
}

/* ---------- 昼夜 ---------- */
/* 响应式分钟时钟：daypart/greetingText 靠它建立依赖，页面开着跨时段也能刷新天色与问候 */
const nowTick = ref<number>(Date.now())
let clockTimer: ReturnType<typeof setInterval> | undefined

const daypart = computed(() => {
  const h = new Date(nowTick.value).getHours()
  if (h >= 5 && h < 9) return 'dawn'
  if (h >= 9 && h < 17) return 'day'
  if (h >= 17 && h < 19) return 'dusk'
  return 'night'
})

/* ---------- 弹层小工具 ---------- */
const sheetTitle = computed(() => {
  switch (overlay.value) {
    case 'parcels': return '📦 今日封存'
    case 'letter': return '✉️ 每日一封'
    case 'stele': return '🏆 观影碑'
    case 'board': return '📢 服主告示牌'
    case 'wall': return '💬 到此一游'
    case 'fortune': return '🎋 每日一签'
    default: return ''
  }
})

function fmtHours(v: unknown): string {
  const s = Number(v ?? 0)
  if (!Number.isFinite(s) || s <= 0) return '0 小时'
  const h = Math.round((s / 3600) * 10) / 10
  return `${h} 小时`
}

function fmtWallTime(v: unknown): string {
  if (typeof v !== 'string' || !v) return ''
  const dt = new Date(v)
  if (Number.isNaN(dt.getTime())) return ''
  const now = new Date()
  const sameDay = dt.toDateString() === now.toDateString()
  const mm = String(dt.getMonth() + 1).padStart(2, '0')
  const dd = String(dt.getDate()).padStart(2, '0')
  const hh = String(dt.getHours()).padStart(2, '0')
  const mi = String(dt.getMinutes()).padStart(2, '0')
  return sameDay ? `${hh}:${mi}` : `${mm}-${dd} ${hh}:${mi}`
}

function onQuest(key: string) {
  if (key === 'checkin') {
    void router.push('/game/checkin')
    return
  }
  if (key === 'parcels') {
    openParcels()
    return
  }
  if (key === 'letter') openLetter()
}

/* ---------- 跳转 ---------- */
function toClassic() {
  setMode('classic')
}

function goLedger() {
  void router.push('/game/ledger')
}

function goAccount() {
  void router.push('/game/account')
}

function goServer() {
  void router.push('/server')
}

function goShop() {
  void router.push('/shop')
}

function goCollector() {
  void router.push('/collector')
}

function goGame() {
  void router.push('/game')
}

async function openCinema() {
  // 用户手势内同步占位新窗口，避免 await 后被弹窗拦截
  const placeholder = window.open('about:blank', '_blank')
  if (!placeholder) {
    useToast().warning('浏览器拦截了新标签页，请允许本站弹窗后重试')
    void router.push('/game/operator')
    return
  }
  try {
    const base = await ensureEmbyBase()
    if (base) {
      placeholder.location.href = base.replace(/\/+$/, '') + '/web/index.html'
      return
    }
  } catch {
    /* 落到兜底 */
  }
  placeholder.close()
  void router.push('/game/operator')
}

/* ---------- 拆包裹 ---------- */
async function onUnseal(id: string | number) {
  const key = String(id)
  if (unsealingIds.value.has(key)) return
  unsealingIds.value.add(key)
  try {
    await panel.unseal(id)
  } catch (e) {
    useToast().error(humanizeError(e))
  } finally {
    unsealingIds.value.delete(key)
  }
}

/* ---------- 拉取 ---------- */
async function loadCheckin() {
  try {
    checkin.value = (await checkinApi.status()) as unknown as CheckinStatus
  } catch {
    checkin.value = null
  }
}

async function loadMember() {
  try {
    memberRaw.value = (await memberApi.info()) as unknown as Record<string, unknown>
  } catch {
    memberRaw.value = null
  }
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && overlay.value !== 'none') closeOverlay()
}

watch(letterDateKey, () => refreshLetterRead())
watch(userId, () => refreshLetterRead())

onMounted(() => {
  void panel.loadHome()
  void panel.loadCollector()
  void loadCheckin()
  void loadMember()
  void loadLeaderboard()
  void loadSteleTop()
  void loadWall()
  void loadFortune()
  refreshLetterRead()
  window.addEventListener('keydown', onKeydown)
  clockTimer = setInterval(() => {
    nowTick.value = Date.now()
  }, 60_000)
})

onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown)
  if (clockTimer !== undefined) {
    clearInterval(clockTimer)
    clockTimer = undefined
  }
})
</script>

<style scoped>
.world-root {
  /* 世界根不在 .v4-scope 内，纸色 token 取不到，就地同步定义（与 v4-paper.css .v4-scope 同值，改值只改那边）；无全局同名的色值/阴影直接写字面值 */
  --paper-card: #FFFDF6;
  --ink: #2A2620;
  --ink2: #6B6257;
  --line: #D8CDB8;
  --paper-primary: #e8a84a;
  --red: #D9482B;
  --green: #4A7C59;

  height: 100dvh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #EFE7D6;
  font-family: "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
  color: var(--ink);
}

.world {
  position: relative;
  width: min(100%, 160dvh);
  aspect-ratio: 1568 / 882;
  overflow: hidden;
  background: linear-gradient(180deg, #f3ead3 0%, #F6F1E7 55%, #efe4cb 100%);
  container-type: inline-size;
  flex: 0 0 auto;
}

.scene {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
}

.hud {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 4.17cqw;
  display: flex;
  align-items: center;
  gap: 0.97cqw;
  padding: 0 1.53cqw;
  background: linear-gradient(180deg, rgba(246, 241, 231, .95), rgba(246, 241, 231, .75) 75%, transparent);
  z-index: 30;
}

.hud .brand {
  font-weight: 800;
  font-size: 1.32cqw;
}

.hud .brand b {
  color: #a06c24;
}

.hud .worldname {
  background: var(--ink);
  color: #FFFDF6;
  border-radius: 999px;
  padding: 0.35cqw 0.97cqw;
  font-size: 0.94cqw;
  font-weight: 700;
  letter-spacing: 0.07cqw;
}

.hud .right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 0.69cqw;
}

.pill {
  background: rgba(255, 253, 246, .92);
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 0.42cqw 0.97cqw;
  font-size: 0.94cqw;
  font-weight: 700;
  box-shadow: 0 1px 3px rgba(42, 38, 32, .08);
  font: inherit;
  color: inherit;
}

button.pill {
  cursor: pointer;
}

.pill.amber {
  background: rgba(185, 128, 47, .12);
  border-color: rgba(185, 128, 47, .35);
  color: #a06c24;
}

.pill .lv {
  color: #a06c24;
}

.avatar {
  width: 2.50cqw;
  height: 2.50cqw;
  border-radius: 50%;
  background: linear-gradient(135deg, #e8a84a, #c97e2c);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 800;
  font-size: 0.94cqw;
  font-family: inherit;
  border: 2px solid #FFFDF6;
  box-shadow: 0 2px 4px rgba(42, 38, 32, .08), 0 12px 32px rgba(42, 38, 32, .13);
  cursor: pointer;
  padding: 0;
}

.mark {
  position: absolute;
  z-index: 20;
  transform: translate(-50%, -100%);
  text-align: center;
  cursor: pointer;
  background: none;
  border: none;
  padding: 0;
  font: inherit;
  color: inherit;
}

.mark .pin {
  margin: 0 auto;
  position: relative;
  transition: transform .15s ease;
}
/* 纸痕测绘：地标本体是手绘 SVG（宽按画布 1568 等比，1 单位 = 100/1568 cqw） */
.mark .pin .art-svg {
  display: block;
  width: 100%;
  height: auto;
  overflow: visible;
}
.mark .art-cinema { width: 9.95cqw; }
.mark .art-post { width: 7.65cqw; }
.mark .art-archive { width: 9.18cqw; }
.mark .art-supply { width: 7.14cqw; }
.mark .art-fortune { width: 8.93cqw; }
.mark .art-walltree { width: 9.44cqw; }
.mark .art-board { width: 6.63cqw; }
.mark .art-stele { width: 6.12cqw; }
.mark .art-collector { width: 8.80cqw; }
.mark .art-lighthouse { width: 6.63cqw; }
.mark .art-hall { width: 7.65cqw; }
/* 夜灯层：部件自带 .nl（暖窗光/光晕），只在夜里显形 */
.mark .art-svg .nl { display: none; }
.world-root.dp-night .mark .art-svg .nl { display: inline; }
/* 场景昼夜小部件：弯月与萤火虫只在夜里，太阳夜里收起 */
.scene .scene-moon, .scene .scene-ff { display: none; }
.world-root.dp-night .scene .scene-moon,
.world-root.dp-night .scene .scene-ff { display: inline; }
.world-root.dp-night .scene .scene-sun { display: none; }
.scene-mobile { display: none; }

.mark .pin::after {
  content: none;
}

.mark .name {
  margin-top: 0.55cqw;
  display: inline-block;
  background: rgba(255, 253, 246, .92);
  color: #5b4632;
  border: 1px solid rgba(42, 38, 32, .10);
  box-shadow: 0 1px 3px rgba(42, 38, 32, .08);
  font-size: 0.94cqw;
  font-weight: 700;
  border-radius: 0.56cqw;
  padding: 0.21cqw 0.69cqw;
  letter-spacing: 0.03cqw;
  white-space: nowrap;
}
.world-root.dp-night .mark .name {
  background: rgba(42, 38, 32, .88);
  color: #F6F1E7;
  border-color: rgba(246, 241, 231, .16);
}

.mark .sub {
  display: block;
  margin-top: 0.28cqw;
  font-size: 0.83cqw;
  color: var(--ink2);
  background: rgba(255, 253, 246, .85);
  border-radius: 0.42cqw;
  padding: 0.07cqw 0.56cqw;
}

.mark .dot {
  position: absolute;
  top: -0.30cqw;
  right: calc(50% - 4.60cqw);
  min-width: 1.46cqw;
  height: 1.46cqw;
  border-radius: 99px;
  background: var(--red);
  color: #fff;
  font-size: 0.83cqw;
  font-weight: 800;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0 0.42cqw;
  border: 2px solid #FFFDF6;
  z-index: 2;
}

.mark:hover .pin {
  transform: translateY(-0.21cqw);
}

.mark:focus-visible,
.pill:focus-visible,
.avatar:focus-visible,
.classic:focus-visible,
.quest .q:focus-visible,
.w-sheet-close:focus-visible {
  outline: 2px solid #a06c24;
  outline-offset: 2px;
}

.player {
  position: absolute;
  z-index: 25;
  transform: translate(-50%, -100%);
  text-align: center;
}

.player .tok {
  width: 2.78cqw;
  height: 2.78cqw;
  margin: 0 auto;
  border-radius: 50%;
  background: linear-gradient(135deg, #e8a84a, #c97e2c);
  border: 0.21cqw solid #FFFDF6;
  box-shadow: 0 4px 14px rgba(42, 38, 32, .3);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 800;
  font-size: 1.11cqw;
}

.player .who {
  margin-top: 0.35cqw;
  font-size: 0.83cqw;
  font-weight: 700;
  background: rgba(255, 253, 246, .9);
  border: 1px solid var(--line);
  padding: 0.07cqw 0.63cqw;
  border-radius: 99px;
  display: inline-block;
}

.feed {
  position: absolute;
  left: 1.53cqw;
  bottom: 1.53cqw;
  width: 21.39cqw;
  background: rgba(255, 253, 246, .94);
  border: 1px solid var(--line);
  border-radius: 1.11cqw;
  box-shadow: 0 2px 4px rgba(42, 38, 32, .08), 0 12px 32px rgba(42, 38, 32, .13);
  padding: 0.97cqw 1.11cqw;
  z-index: 30;
}

.feed h4 {
  font-size: 0.90cqw;
  letter-spacing: 0.10cqw;
  color: var(--ink2);
  margin-bottom: 0.56cqw;
}

.feed h4 i {
  color: var(--green);
  font-style: normal;
}

.feed ul {
  margin: 0;
  padding: 0;
}

.feed li {
  list-style: none;
  font-size: 0.90cqw;
  color: var(--ink2);
  padding: 0.42cqw 0;
  border-top: 1px dashed var(--line);
}

.feed li:first-of-type {
  border-top: none;
}

.feed li b {
  color: var(--ink);
  margin-right: 0.42cqw;
}

.quest {
  position: absolute;
  right: 1.53cqw;
  bottom: 1.53cqw;
  width: 19.72cqw;
  background: rgba(255, 253, 246, .94);
  border: 1px solid var(--line);
  border-radius: 1.11cqw;
  box-shadow: 0 2px 4px rgba(42, 38, 32, .08), 0 12px 32px rgba(42, 38, 32, .13);
  padding: 0.97cqw 1.11cqw;
  z-index: 30;
}

.quest .stamp-done { position: absolute; top: -11px; right: 12px; transform: rotate(-6deg); border: 2px solid var(--red); color: var(--red); background: rgba(255,253,246,.92); border-radius: 6px; padding: 2px 9px; font-size: 11px; font-weight: 800; letter-spacing: 1px; }
.mark.fortune-pending .pin { animation: fortunePulse 2.2s ease-in-out infinite; }
@keyframes fortunePulse { 0% { filter: drop-shadow(0 0 2px rgba(242,192,99,.0)); } 50% { filter: drop-shadow(0 0 9px rgba(242,192,99,.75)); } 100% { filter: drop-shadow(0 0 2px rgba(242,192,99,0)); } }
.m-feed { background: rgba(255,253,246,.94); border: 1px solid var(--line); border-radius: 99px; padding: 7px 14px; font-size: 12px; color: var(--ink2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.m-feed i { color: var(--green); font-style: normal; margin-right: 4px; }

.quest h4 {
  font-size: 0.90cqw;
  letter-spacing: 0.10cqw;
  color: var(--ink2);
  margin-bottom: 0.42cqw;
}

.quest .q {
  display: flex;
  align-items: center;
  gap: 0.63cqw;
  font-size: 0.94cqw;
  padding: 0.49cqw 0;
  border: none;
  border-top: 1px dashed var(--line);
  background: none;
  width: 100%;
  text-align: left;
  font-family: inherit;
  color: inherit;
  cursor: pointer;
}

.quest .q:first-of-type {
  border-top: none;
}

.quest .box {
  width: 1.32cqw;
  height: 1.32cqw;
  border-radius: 0.42cqw;
  border: 1.5px solid #b9ac8f;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 0.83cqw;
  color: #fff;
  flex: 0 0 auto;
}

.quest .q.done {
  color: var(--ink2);
}

.quest .q.done .box {
  background: var(--green);
  border-color: var(--green);
}

.quest .q .rw {
  margin-left: auto;
  font-size: 0.83cqw;
  color: #a06c24;
  font-weight: 700;
}

.quest .bar {
  height: 0.42cqw;
  background: #EFE7D6;
  border-radius: 99px;
  margin-top: 0.69cqw;
  overflow: hidden;
}

.quest .bar i {
  display: block;
  height: 100%;
  background: var(--paper-primary);
  border-radius: 99px;
  transition: width .3s ease;
}

.hello {
  position: absolute;
  left: 1.53cqw;
  top: 5.42cqw;
  z-index: 30;
  background: rgba(255, 253, 246, .94);
  border: 1px solid var(--line);
  border-radius: 1.11cqw;
  box-shadow: 0 2px 4px rgba(42, 38, 32, .08), 0 12px 32px rgba(42, 38, 32, .13);
  padding: 0.97cqw 1.25cqw;
  max-width: 22.92cqw;
}

.hello .d {
  font-size: 0.83cqw;
  letter-spacing: 0.14cqw;
  color: var(--ink2);
}

.hello h2 {
  font-size: 1.46cqw;
  margin: 0.21cqw 0 0.14cqw;
}

.hello p {
  font-size: 0.90cqw;
  color: var(--ink2);
  line-height: 1.55;
}

.hello p b {
  color: var(--red);
}

.classic {
  position: absolute;
  right: 1.53cqw;
  top: 5.42cqw;
  z-index: 30;
  background: rgba(255, 253, 246, .9);
  border: 1px solid var(--line);
  color: var(--ink2);
  border-radius: 999px;
  padding: 0.49cqw 1.04cqw;
  font-size: 0.90cqw;
  font-weight: 600;
  font-family: inherit;
  box-shadow: 0 1px 3px rgba(42, 38, 32, .08);
  cursor: pointer;
}

.w-overlay {
  position: fixed;
  inset: 0;
  background: rgba(42, 38, 32, .45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 80;
  padding: 24px;
}

.w-sheet {
  background: #FFFDF6;
  border: 1px solid #D8CDB8;
  border-radius: 16px;
  box-shadow: 0 2px 4px rgba(42, 38, 32, .08), 0 12px 32px rgba(42, 38, 32, .13);
  width: min(560px, calc(100vw - 48px));
  max-height: 84vh;
  max-height: 84dvh;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 20px;
  color: #2A2620;
  font-family: "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
}

.w-sheet-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}

.w-sheet-title {
  font-size: 16px;
  font-weight: 800;
}

.w-sheet-close {
  background: none;
  border: 1px solid #D8CDB8;
  border-radius: 999px;
  width: 30px;
  height: 30px;
  cursor: pointer;
  color: #6B6257;
  font-size: 13px;
  line-height: 1;
}

.w-sheet-empty {
  font-size: 13.5px;
  color: #6B6257;
  padding: 12px 0;
}

.w-parcel-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}

/* 面单卡原为横滑条设计、定高 216px，在弹层里内容稀疏显空 → 弹层内改自然高度紧凑排布 */
.w-parcel-grid { align-items: flex-start; }
.w-parcel-grid .v4-parcel { min-height: 0; }
.w-parcel-grid .v4-parcel-waybill { flex: 0 1 auto; justify-content: flex-start; }

/* ---------- 昼夜天色 ---------- */
.dp-dawn .world {
  background: linear-gradient(180deg, #f8e2bd 0%, #F6F1E7 55%, #f2e0bd 100%);
}
.dp-dusk .world {
  background: linear-gradient(180deg, #f3cf9e 0%, #efdfc2 55%, #e9d0a8 100%);
}
.dp-night .world {
  background: linear-gradient(180deg, #2e3a5c 0%, #3d4a68 55%, #333a52 100%);
}
.dp-dawn .scene { filter: brightness(.97) sepia(.14); }
.dp-day .scene { filter: none; }
.dp-dusk .scene { filter: brightness(.9) sepia(.22); }
.dp-night .scene { filter: brightness(.45) saturate(.95); }
.night-veil { display: none; }
.dp-night .night-veil {
  display: block;
  position: absolute;
  inset: 0;
  z-index: 5;
  pointer-events: none;
  background: linear-gradient(180deg, rgba(26, 38, 92, .72) 0%, rgba(26, 38, 92, .66) 60%, rgba(26, 38, 92, .70) 100%);
}
.veil-moon { position: absolute; overflow: visible; filter: drop-shadow(0 0 9px rgba(247, 240, 207, .55)); }
.veil-moon.moon-d { left: 27.4%; top: 7%; width: 46px; height: 46px; }
.veil-moon.moon-m { display: none; }
@media (max-width: 1023px) {
  .veil-moon.moon-d { display: none; }
  .veil-moon.moon-m { display: block; left: 9%; top: 8.6%; width: 38px; height: 38px; }
}
.world-root.dp-night .mark .art-svg { filter: brightness(.86) saturate(.92);
}
.dp-night .world::after {
  /* 星空点缀（纯 CSS，不新增图片） */
  content: "";
  position: absolute;
  inset: 0;
  pointer-events: none;
  background:
    radial-gradient(circle at 12% 14%, rgba(255, 246, 220, .95) 0 2px, transparent 3px),
    radial-gradient(circle at 28% 8%, rgba(255, 246, 220, .8) 0 1.5px, transparent 2.5px),
    radial-gradient(circle at 45% 18%, rgba(255, 246, 220, .9) 0 2px, transparent 3px),
    radial-gradient(circle at 62% 10%, rgba(255, 246, 220, .7) 0 1.5px, transparent 2.5px),
    radial-gradient(circle at 78% 22%, rgba(255, 246, 220, .85) 0 2px, transparent 3px),
    radial-gradient(circle at 92% 8%, rgba(255, 246, 220, .9) 0 1.5px, transparent 2.5px),
    radial-gradient(circle at 8% 32%, rgba(255, 246, 220, .6) 0 1.5px, transparent 2.5px),
    radial-gradient(circle at 55% 30%, rgba(255, 246, 220, .55) 0 1.5px, transparent 2.5px);
}
.dp-night .world::before {
  /* 夜色罩：地图整体压暗，纸色建筑依然可辨 */
  content: "";
  position: absolute;
  inset: 0;
  z-index: 20;
  pointer-events: none;
  background: rgba(20, 28, 52, .12);
}
.dp-night .lighthouse .pin {
  /* 夜晚灯塔亮灯 */
  filter: drop-shadow(0 0 10px rgba(255, 214, 120, .85));
}

/* ---------- 新弹层内容：排行榜 / 告示牌 / 留言墙 / 抽签 ---------- */
.w-sheet.is-stele {
  background: radial-gradient(120% 90% at 50% 0%, #FFFDF6 0%, #FCF8EC 70%, #FAF3DF 100%);
  position: relative;
}
.lb-head { text-align: center; margin-bottom: 14px; position: relative; }
.lb-arch { display: flex; justify-content: center; margin-bottom: 4px; }
.lb-title {
  font-family: "Songti SC", "STSong", "SimSun", serif;
  font-size: 25px; font-weight: 700; letter-spacing: .34em; text-indent: .34em; color: #2A2620;
}
.lb-sub { margin-top: 4px; font-size: 12px; color: #6B6257; letter-spacing: .22em; }
.lb-sub b { color: #a06c24; font-weight: 600; }
.lb-close { position: absolute; top: -4px; right: -2px; display: flex; align-items: center; justify-content: center; }
.lb-tabs { display: flex; gap: 10px; justify-content: center; margin-bottom: 10px; }
.lb-tab {
  display: inline-flex; align-items: center; gap: 7px;
  padding: 8px 18px; border-radius: 999px;
  border: 1.4px solid #DCCFA9; background: #FFFDF6;
  font-size: 14.5px; color: #6B6257; letter-spacing: .06em;
  font-family: inherit; cursor: pointer;
}
.lb-tab.on {
  background: linear-gradient(180deg, #FDF3D8, #FBEECB);
  border-color: #e8a84a; color: #a06c24; font-weight: 700;
  box-shadow: 0 2px 8px rgba(201, 126, 44, .18);
}
.lb-periods { display: flex; justify-content: flex-end; align-items: center; margin: -2px 2px 4px; }
.lb-plabel { font-size: 11.5px; color: #9A9184; letter-spacing: .18em; margin-right: 8px; }
.lb-period {
  font-size: 12.5px; padding: 4px 13px; color: #6B6257;
  border: 1.2px solid #DCCFA9; background: #FFFDF6;
  font-family: inherit; cursor: pointer;
}
.lb-period:first-of-type { border-radius: 8px 0 0 8px; border-right: none; }
.lb-period:last-of-type { border-radius: 0 8px 8px 0; }
.lb-period.on { background: #2e3a5c; border-color: #2e3a5c; color: #F3EDD9; font-weight: 600; }
.lb-podium { display: flex; gap: 12px; align-items: flex-end; margin: 8px 0 2px; }
.lb-pod { flex: 1; text-align: center; min-width: 0; }
.lb-pod.r1 { order: 2; }
.lb-pod.r2 { order: 1; }
.lb-pod.r3 { order: 3; }
.lb-medal {
  position: relative; width: 52px; height: 52px; margin: 0 auto 6px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-family: "Songti SC", "STSong", serif; font-size: 21px; font-weight: 700;
  background: radial-gradient(circle at 34% 30%, #FFFDF6, #F7EFDA);
}
.lb-pod.r1 .lb-medal {
  width: 62px; height: 62px; font-size: 26px; color: #a06c24;
  border: 2.2px solid #e8a84a;
  box-shadow: 0 0 0 3px #FDF3D8, 0 4px 12px rgba(201, 126, 44, .25);
}
.lb-pod.r2 .lb-medal { color: #5F7A8C; border: 2px solid #a8c6dd; box-shadow: 0 0 0 3px #EFF5FA; }
.lb-pod.r3 .lb-medal { color: #A05C2C; border: 2px solid #D29A63; box-shadow: 0 0 0 3px #FAEEE1; }
.lb-crown { position: absolute; top: -16px; left: 50%; transform: translateX(-50%); }
.lb-sealdot {
  position: absolute; right: -4px; bottom: -2px; width: 17px; height: 17px; border-radius: 5px;
  background: #D9482B; color: #FFF6E8; font-size: 10px; font-weight: 700;
  display: flex; align-items: center; justify-content: center; transform: rotate(-8deg);
  box-shadow: 0 2px 5px rgba(217, 72, 43, .35);
}
.lb-pname {
  font-size: 15px; font-weight: 700; line-height: 1.3;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.lb-pod.r1 .lb-pname { font-size: 16.5px; }
.lb-ptype { font-size: 11px; color: #9A9184; margin-top: 2px; letter-spacing: .14em; }
.lb-pnum { margin-top: 3px; font-size: 13.5px; font-weight: 700; color: #a06c24; }
.lb-pnum small { font-size: 11px; font-weight: 500; color: #6B6257; }
.lb-plinth {
  margin: 9px auto 0; height: 8px; border-radius: 5px 5px 2px 2px;
  background: linear-gradient(180deg, #d9cba6, #CBBd97);
  box-shadow: 0 1px 0 #fff inset; width: 76%;
}
.lb-pod.r1 .lb-plinth { width: 86%; height: 11px; }
.lb-divider { display: flex; align-items: center; gap: 10px; margin: 14px 4px 4px; }
.lb-divider::before, .lb-divider::after { content: ""; flex: 1; border-top: 1.4px dashed #DCCFA9; }
.lb-divider i { width: 7px; height: 7px; background: #e8a84a; transform: rotate(45deg); display: block; }
.lb-rows { list-style: none; margin: 0; padding: 0; }
.lb-row {
  display: flex; align-items: center; gap: 12px;
  padding: 9px 10px; border-bottom: 1px solid #E7DCC3;
}
.lb-row:last-child { border-bottom: none; }
.lb-rk {
  width: 26px; text-align: center; flex: none;
  font-family: "Songti SC", "STSong", serif; font-size: 16px; color: #9A9184; font-weight: 700;
}
.lb-rname { flex: 1; min-width: 0; font-size: 14.5px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.lb-tag {
  flex: none; font-size: 10.5px; color: #6B6257; letter-spacing: .1em;
  border: 1.1px solid #DCCFA9; border-radius: 999px; padding: 2px 8px; background: #FFFDF6;
}
.lb-metag {
  flex: none; font-size: 10.5px; font-weight: 700; color: #fff; background: #7d9b76;
  border-radius: 5px; padding: 2px 6px; transform: rotate(-3deg);
}
.lb-rnum { flex: none; font-size: 14px; font-weight: 700; color: #a06c24; }
.lb-rnum small { font-size: 11px; font-weight: 500; color: #6B6257; }
.lb-row.me {
  background: linear-gradient(90deg, rgba(147, 173, 139, .20), rgba(147, 173, 139, .07));
  border-radius: 10px; border-bottom-color: transparent;
  box-shadow: inset 3px 0 0 #7d9b76;
}
.lb-myrank {
  margin-top: 14px; border: 1.4px dashed #c97e2c; border-radius: 14px;
  background: linear-gradient(180deg, #FFFDF6, #FBF3DC);
  padding: 11px 16px; display: flex; align-items: center; gap: 13px;
}
.lb-stamp {
  flex: none; width: 46px; height: 46px; border-radius: 50%;
  border: 1.8px solid #D9482B; color: #D9482B;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  transform: rotate(-7deg); background: rgba(217, 72, 43, .05);
}
.lb-stamp b { font-family: "Songti SC", "STSong", serif; font-size: 16px; line-height: 1; }
.lb-stamp span { font-size: 8.5px; letter-spacing: .2em; margin-top: 2px; }
.lb-mtxt { flex: 1; font-size: 13.5px; color: #6B6257; line-height: 1.55; }
.lb-mtxt b { color: #2A2620; font-size: 15px; }
.lb-mtxt .gap { color: #a06c24; font-weight: 700; }
.lb-scope { margin-top: 10px; text-align: center; font-size: 11px; color: #9A9184; letter-spacing: .05em; }
.w-board-text {
  font-size: 15px;
  line-height: 1.9;
  white-space: pre-wrap;
  margin: 4px 0 10px;
}
.w-board-sig {
  text-align: right;
  font-size: 13px;
  color: var(--ink2);
}
.w-wall-list {
  list-style: none;
  margin: 0 0 12px;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 40dvh;
  overflow-y: auto;
}
.w-wall-row {
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 8px 12px;
  background: var(--paper-card);
}
.w-wall-nick {
  font-size: 13px;
  font-weight: 800;
  color: #a06c24;
}
.w-wall-time {
  font-size: 11px;
  color: var(--ink2);
  margin-left: 8px;
}
.w-wall-text {
  margin: 4px 0 0;
  font-size: 14px;
  line-height: 1.7;
}
.w-wall-form {
  display: flex;
  gap: 8px;
  margin-top: 4px;
}
.w-wall-form input {
  flex: 1;
  min-width: 0;
  padding: 9px 12px;
  border-radius: 10px;
  border: 1px solid var(--line);
  background: #FFFDF6;
  font-size: 14px;
  color: var(--ink);
}
.w-wall-btn, .w-fortune-btn {
  padding: 9px 18px;
  border-radius: 10px;
  border: none;
  background: #a06c24;
  color: #FFFDF6;
  font-size: 14px;
  font-weight: 800;
  cursor: pointer;
  white-space: nowrap;
}
.w-wall-btn:disabled, .w-fortune-btn:disabled {
  opacity: .5;
  cursor: default;
}
.w-fortune {
  text-align: center;
  padding: 10px 0 4px;
}
.w-fortune-level {
  display: inline-block;
  font-size: 22px;
  font-weight: 900;
  color: #D9482B;
  border: 2px solid #D9482B;
  border-radius: 12px;
  padding: 6px 22px;
  margin-bottom: 12px;
  transform: rotate(-3deg);
}
.w-fortune-text {
  font-size: 16px;
  line-height: 1.9;
  margin: 0 0 10px;
}
.w-fortune-yiji {
  font-size: 14px;
  color: var(--ink2);
}
.w-fortune-yiji b {
  color: var(--green);
}
.w-fortune-draw {
  text-align: center;
  padding: 16px 0 6px;
}
.w-fortune-pot {
  font-size: 52px;
  margin-bottom: 6px;
}

/* /game 页内嵌：不定高，留呼吸边距（手机端另有布局） */
.world-root.is-game { height: auto; padding: 16px 0; }

/* ---------- 手机端 /game：竖屏长地图 + 底部抽屉（≤1023px，仅 is-game 生效） ---------- */
@media (max-width: 1023px) {
  .world-root.is-game { display: block; height: auto; }
  .world-root.is-game .world {
    width: 100%;
    aspect-ratio: 390 / 700;
    overflow: hidden;
  }
  /* 手机端用竖版场景（780×1400），与手机地标坐标同属一套图纸 */
  .world-root.is-game .scene-desktop { display: none; }
  .world-root.is-game .scene-mobile { display: block; }
  /* 地标按竖版画布 780 等比（1 单位 = 0.5px）：宽随各地标 viewBox */
  .world-root.is-game .mark .art-cinema { width: 78px; }
  .world-root.is-game .mark .art-post { width: 60px; }
  .world-root.is-game .mark .art-archive { width: 72px; }
  .world-root.is-game .mark .art-supply { width: 56px; }
  .world-root.is-game .mark .art-fortune { width: 70px; }
  .world-root.is-game .mark .art-walltree { width: 74px; }
  .world-root.is-game .mark .art-board { width: 52px; }
  .world-root.is-game .mark .art-stele { width: 48px; }
  .world-root.is-game .mark .art-collector { width: 69px; }
  .world-root.is-game .mark .art-lighthouse { width: 52px; }
  .world-root.is-game .mark .art-hall { width: 60px; }
  .world-root.is-game .mark .name { font-size: 13.5px; padding: 4px 11px; border-radius: 8px; margin-top: 7px; }
  .world-root.is-game .mark .sub { font-size: 11px; padding: 2px 8px; border-radius: 6px; margin-top: 4px; width: max-content; max-width: 56vw; margin-left: auto; margin-right: auto; }
  .world-root.is-game .mark .dot { min-width: 22px; height: 22px; font-size: 12px; top: -6px; right: calc(50% - 44px); padding: 0 6px; }
  .world-root.is-game .mark:hover .pin { transform: none; }
  /* 手机坐标（!important 覆盖行内桌面坐标；照手机图纸百分比） */
  .world-root.is-game .m-lighthouse { left: 77.5% !important; top: 82.3% !important; }
  .world-root.is-game .m-cinema { left: 50% !important; top: 53% !important; }
  .world-root.is-game .m-gamehall { left: 50.8% !important; top: 90.3% !important; }
  .world-root.is-game .m-post { left: 22.6% !important; top: 39.2% !important; }
  .world-root.is-game .m-parcels { left: 23.3% !important; top: 66.8% !important; }
  .world-root.is-game .m-shop { left: 25.1% !important; top: 82.6% !important; }
  .world-root.is-game .m-collector { left: 76.7% !important; top: 99.2% !important; }
  .world-root.is-game .m-board { left: 22.6% !important; top: 99% !important; }
  .world-root.is-game .m-stele { left: 50% !important; top: 26% !important; }
  .world-root.is-game .m-wall { left: 76.4% !important; top: 38.5% !important; }
  .world-root.is-game .m-fortune { left: 75.9% !important; top: 60.3% !important; }
  .world-root.is-game .m-player { left: 50% !important; top: 68% !important; }
  .world-root.is-game .m-player .tok { width: 44px; height: 44px; font-size: 16px; border-width: 3px; }
  .world-root.is-game .m-player .who { font-size: 12px; padding: 2px 10px; }
  /* 分区牌 */
  .world-root.is-game .m-zone { position: absolute; z-index: 26; font-size: 13px; font-weight: 800; color: #a06c24; background: rgba(255,253,246,.9); border: 1.5px dashed #a06c24; border-radius: 10px; padding: 4px 12px; letter-spacing: 1px; left: 10px; top: 118px; }
  .world-root.is-game .m-zone-2 { top: 57%; left: 50%; transform: translate(-50%, -50%); }
  .world-root.is-game .m-zone-line { position: absolute; z-index: 4; left: 14px; right: 14px; top: 57%; border-top: 2px dashed rgba(160,108,36,.5); }
  /* 滑动提示 */
  .world-root.is-game .m-scroll-hint { display: none; }
  .world-root.is-game .m-scroll-hint span { display: inline-block; background: #2A2620; color: #FFFDF6; border-radius: 999px; padding: 7px 18px; font-size: 13px; font-weight: 700; }
  /* 紧凑 HUD：问候 + 连签/积分/Lv 两行；桌面 hello 卡与 hud 在手机端隐藏 */
  .world-root.is-game .hello, .world-root.is-game .hud { display: none; }
  .world-root.is-game .m-hud { display: block; position: absolute; top: 0; left: 0; right: 0; z-index: 40; padding: 12px 14px 18px; background: linear-gradient(180deg, rgba(246,241,231,.97), rgba(246,241,231,.7) 70%, transparent); }
  .world-root.is-game .game-goalbar { margin: 0 0 10px; background: rgba(255,253,246,.96); border: 1px solid var(--line); border-radius: 12px; padding: 8px 12px; box-shadow: 0 2px 4px rgba(42,38,32,.08); }
  .world-root.is-game .m-goal-text { font-size: 12px; font-weight: 700; }
  .world-root.is-game .m-goal-text b { color: #a06c24; }
  .world-root.is-game .m-goal-bar { height: 6px; background: #EFE7D6; border-radius: 99px; margin-top: 6px; overflow: hidden; }
  .world-root.is-game .m-goal-bar i { display: block; height: 100%; background: var(--paper-primary); border-radius: 99px; transition: width .3s ease; }
  .world-root.is-game .m-feed { display: none; }
  .world-root.is-game .m-hello { font-size: 14px; font-weight: 800; }
  .world-root.is-game .m-stats { font-size: 11.5px; color: #6B6257; margin-top: 2px; }
  .world-root.is-game .m-stats b { color: #a06c24; }
  /* 世界动态/今日任务两块浮动卡：手机端不占地图，隐藏（数据不变，只是排版） */
  .world-root.is-game .feed, .world-root.is-game .quest { display: none; }
  /* 底部抽屉：占屏高约 70%，顶部圆角+下拉把手 */
  .world-root.is-game .w-overlay { align-items: flex-end; padding: 0; }
  .world-root.is-game .w-sheet { width: 100%; max-height: 72vh; max-height: 72dvh; border-radius: 24px 24px 0 0; border-bottom: none; padding: 8px 20px calc(24px + env(safe-area-inset-bottom)); overscroll-behavior: contain; }
  .world-root.is-game .w-grip { width: 44px; height: 5px; border-radius: 99px; background: #D8CDB8; margin: 6px auto 8px; }
  .world-root.is-game .w-sheet-head { margin-bottom: 10px; }
  /* 手机弹层里封存卡两列等宽铺满（网格 gap 12px） */
  .world-root.is-game .w-parcel-grid .v4-parcel { width: calc(50% - 6px); }
}
/* 桌面端隐藏手机专用元素 */
.m-zone, .m-zone-line, .m-scroll-hint, .w-grip, .m-hud, .game-goalbar, .m-feed { display: none; }
@media (max-width: 1023px) {
  .world-root.is-game .m-zone, .world-root.is-game .m-zone-line,
  .world-root.is-game .w-grip,
  .world-root.is-game .m-hud, .world-root.is-game .game-goalbar { display: block; }
}

@media (prefers-reduced-motion: reduce) {
  .mark .pin {
    transition: none;
  }
  .mark.fortune-pending .pin {
    animation: none;
  }
  .mark:hover .pin {
    transform: none;
  }
  .quest .bar i {
    transition: none;
  }
}
</style>
