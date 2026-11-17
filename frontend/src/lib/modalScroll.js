let locks = 0, previous = '';
export function lockPageScroll() { if(locks++ === 0) { previous = document.body.style.overflow; document.body.style.overflow = 'hidden'; } }
export function unlockPageScroll() { locks = Math.max(0, locks - 1); if(!locks) document.body.style.overflow = previous; }
