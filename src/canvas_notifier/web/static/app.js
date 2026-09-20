const previewButton = document.querySelector('#preview-button');
if (previewButton) {
  previewButton.addEventListener('click', async () => {
    previewButton.disabled = true;
    const result = document.querySelector('#preview-result');
    result.textContent = '正在计算提醒时间…';
    try {
      const response = await fetch('/api/rules/preview', {method: 'POST', body: new FormData(document.querySelector('#rule-form'))});
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '预览失败，请检查输入。');
      result.textContent = data.items.length ? data.items.map(item => `${item.scheduled_at} · ${item.label} · ${item.title}`).join('\n') : '此范围暂无未来提醒：任务可能已完成、时间已过，或对应偏移已关闭。';
    } catch (error) {
      result.textContent = error.message;
    } finally {
      previewButton.disabled = false;
    }
  });
}
