/** Keep short notes at one input row while allowing complete multiline notes. */
export function resizeRemarkTextarea(element, maxRows = 3) {
  if (!element) return;
  const styles = window.getComputedStyle(element);
  const number = value => Number.parseFloat(value) || 0;
  const lineHeight = number(styles.lineHeight) || number(styles.fontSize) * 1.5 || 24;
  const padding = number(styles.paddingTop) + number(styles.paddingBottom);
  const border = number(styles.borderTopWidth) + number(styles.borderBottomWidth);
  const minimum = Math.max(50, number(styles.minHeight));
  const maximum = Math.max(minimum, lineHeight * Math.max(1, maxRows) + padding + border);

  // Reset before measuring so deleting text and widening the window can shrink it.
  element.style.height = 'auto';
  const contentHeight = element.scrollHeight + border;
  element.style.height = `${Math.max(minimum, Math.min(contentHeight, maximum))}px`;
  element.style.overflowY = contentHeight > maximum ? 'auto' : 'hidden';
}
