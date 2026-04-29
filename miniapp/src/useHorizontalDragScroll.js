import { useEffect, useRef } from 'react';

export function useHorizontalDragScroll() {
  const ref = useRef(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;

    let pointerId = null;
    let isMouseDragging = false;
    let startX = 0;
    let startScrollLeft = 0;
    let didDrag = false;
    let suppressClick = false;

    const hasHorizontalOverflow = () => el.scrollWidth > el.clientWidth + 1;

    const resetDrag = () => {
      pointerId = null;
      isMouseDragging = false;
      didDrag = false;
      el.classList.remove('drag-scroll-active');
    };

    const beginDrag = (clientX) => {
      startX = clientX;
      startScrollLeft = el.scrollLeft;
      didDrag = false;
      el.classList.add('drag-scroll-active');
    };

    const moveDrag = (clientX, event) => {
      const deltaX = clientX - startX;
      if (Math.abs(deltaX) > 4) {
        didDrag = true;
      }
      if (didDrag) {
        el.scrollLeft = startScrollLeft - deltaX;
        event.preventDefault();
      }
    };

    const finishDrag = () => {
      if (didDrag) {
        suppressClick = true;
        window.setTimeout(() => {
          suppressClick = false;
        }, 0);
      }
      resetDrag();
    };

    const onPointerDown = (event) => {
      if (event.pointerType !== 'mouse' || event.button !== 0 || !hasHorizontalOverflow()) {
        return;
      }
      pointerId = event.pointerId;
      beginDrag(event.clientX);
      el.setPointerCapture?.(event.pointerId);
    };

    const onPointerMove = (event) => {
      if (event.pointerId !== pointerId) return;
      moveDrag(event.clientX, event);
    };

    const onPointerEnd = (event) => {
      if (event.pointerId !== pointerId) return;
      el.releasePointerCapture?.(event.pointerId);
      finishDrag();
    };

    const onMouseDown = (event) => {
      if (event.button !== 0 || pointerId !== null || !hasHorizontalOverflow()) {
        return;
      }
      isMouseDragging = true;
      beginDrag(event.clientX);
    };

    const onMouseMove = (event) => {
      if (!isMouseDragging) return;
      moveDrag(event.clientX, event);
    };

    const onMouseUp = () => {
      if (!isMouseDragging) return;
      finishDrag();
    };

    const onClickCapture = (event) => {
      if (!suppressClick) return;
      event.preventDefault();
      event.stopPropagation();
      suppressClick = false;
    };

    const onWheel = (event) => {
      if (!hasHorizontalOverflow() || Math.abs(event.deltaX) >= Math.abs(event.deltaY)) {
        return;
      }
      const nextLeft = el.scrollLeft + event.deltaY;
      const maxLeft = el.scrollWidth - el.clientWidth;
      if ((event.deltaY < 0 && el.scrollLeft <= 0) || (event.deltaY > 0 && el.scrollLeft >= maxLeft)) {
        return;
      }
      el.scrollLeft = nextLeft;
      event.preventDefault();
    };

    el.classList.add('drag-scroll');
    el.addEventListener('pointerdown', onPointerDown);
    el.addEventListener('pointermove', onPointerMove);
    el.addEventListener('pointerup', onPointerEnd);
    el.addEventListener('pointercancel', onPointerEnd);
    el.addEventListener('mousedown', onMouseDown);
    window.addEventListener('mousemove', onMouseMove);
    window.addEventListener('mouseup', onMouseUp);
    el.addEventListener('click', onClickCapture, true);
    el.addEventListener('wheel', onWheel, { passive: false });

    return () => {
      el.classList.remove('drag-scroll', 'drag-scroll-active');
      el.removeEventListener('pointerdown', onPointerDown);
      el.removeEventListener('pointermove', onPointerMove);
      el.removeEventListener('pointerup', onPointerEnd);
      el.removeEventListener('pointercancel', onPointerEnd);
      el.removeEventListener('mousedown', onMouseDown);
      window.removeEventListener('mousemove', onMouseMove);
      window.removeEventListener('mouseup', onMouseUp);
      el.removeEventListener('click', onClickCapture, true);
      el.removeEventListener('wheel', onWheel);
    };
  }, []);

  return ref;
}
