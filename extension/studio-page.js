/* DOM-only adapter. Unknown UI states stop the uploader instead of guessing. */
function videoDropStudioPage(command, data = {}) {
  if (location.origin !== 'https://studio.youtube.com') return {blocked: 'Войдите в YouTube в открытой вкладке Chrome.'};
  const text = element => (element?.innerText || element?.textContent || '').replace(/\s+/g, ' ').trim();
  const visible = element => !!element && !element.closest('[hidden],[aria-hidden="true"]') &&
    getComputedStyle(element).visibility !== 'hidden' && (element.getClientRects().length > 0 ||
      [...element.children].some(child => child.getClientRects().length > 0));
  const all = (selector, root = document) => [...root.querySelectorAll(selector)].filter(visible);
  const one = (selector, root = document) => {const values=all(selector,root);return values.length===1?values[0]:null;};
  const disabled = element => !element || element.hasAttribute('disabled') || element.getAttribute('aria-disabled') === 'true' ||
    element.disabled === true || element.querySelector('button')?.disabled === true;
  const press = element => {if (!element || disabled(element)) return false;(element.querySelector('button') || element).click();return true;};
  const exactButton = (labels, root=document) => {
    const candidates=all('ytcp-button,ytcp-icon-button,[role="menuitem"]',root).filter(e=>labels.includes(text(e))||labels.includes(e.getAttribute('aria-label')));
    return candidates.length===1?candidates[0]:null;
  };
  const channelHref=document.querySelector('a#home-button[href^="/channel/"]')?.getAttribute('href') || '';
  const channelId=channelHref.match(/^\/channel\/(UC[\w-]{22})(?:\/|$)/)?.[1] || '';
  if (data.channelId && channelId && data.channelId !== channelId) return {blocked:'В Chrome выбран другой канал. Переключите аккаунт YouTube; файл сохранён на Mac.'};
  const dialog=one('ytcp-uploads-dialog');
  const scope=dialog || document;
  const radio=name=>one('[role="radio"][name="'+name+'"]',scope);
  const selected=name=>radio(name)?.getAttribute('aria-checked')==='true';
  const titleBox=all('[contenteditable="true"][role="textbox"]',scope).find(e=>/^(Укажите название|Add a title|Enter a title)/i.test(e.getAttribute('aria-label')||''));
  const next=dialog && one('#next-button',dialog), done=dialog && one('#done-button',dialog);
  const kidName=data.madeForKids?'VIDEO_MADE_FOR_KIDS_MFK':'VIDEO_MADE_FOR_KIDS_NOT_MFK';
  if(command!=='snapshot'&&!channelId) return {blocked:'Studio ещё не показал канал. Завершите вход в YouTube.'};
  if(command==='create')return {clicked:press(exactButton(['Создать','Create']))};
  if(command==='upload-menu')return {clicked:press(exactButton(['Добавить видео','Upload videos']))};
  if(command==='details'){
    if(!dialog||!titleBox||!radio(kidName))return {clicked:false};
    if(text(titleBox)!==data.title){titleBox.focus();titleBox.textContent=data.title;titleBox.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:data.title}));titleBox.dispatchEvent(new Event('change',{bubbles:true}));}
    if(!selected(kidName))press(radio(kidName));
    return {clicked:true};
  }
  if(command==='next')return {clicked:press(next)};
  if(command==='unlisted')return {clicked:press(radio('UNLISTED'))};
  if(command==='save'){
    // Explicitly selected UNLISTED is required; never rely on a channel's upload defaults.
    if(!dialog||!selected('UNLISTED')||selected('PUBLIC')||selected('PRIVATE')||!['Сохранить','Save'].includes(text(done)))return {blocked:'Не подтверждён доступ «По ссылке». Сохранение остановлено.'};
    return {clicked:press(done)};
  }
  const ids=[...new Set(all('a[href]',scope).map(e=>e.href.match(/^https:\/\/(?:youtu\.be\/|www\.youtube\.com\/watch\?v=)([\w-]{11})(?:[?&]|$)/)?.[1]).filter(Boolean))];
  const editorId=location.pathname.match(/^\/video\/([\w-]{11})\/edit$/)?.[1] || '';
  const visibility=text(one('#visibility-text'));
  const badges=all('#video-resolutions [role="img"][aria-label]');
  const processed=badges.some(e=>e.id==='badge-sd') && badges.every(e=>/^(Обработка версии .+ завершена|(?:SD|HD|4K) processing complete)$/i.test(e.getAttribute('aria-label')));
  const save=one('ytcp-button#save');
  const errors=all('#error-message,ytcp-uploads-dialog [role="alert"]',scope).map(text).filter(Boolean);
  const progress=dialog?all('ytcp-video-upload-progress,ytcp-video-upload-progress-v2,#upload-status,#upload-progress,#processing-status,#progress-label',dialog).map(text).join(' '):'';
  const uploadComplete=/(?:Загрузка завершена|Обработка завершена|Upload complete|Processing complete)/i.test(progress);
  const finished=all('ytcp-video-share-dialog').length>0 || all('[role="dialog"] h2,[role="dialog"] #title')
    .some(element=>['Видео опубликовано','Видео загружено','Video published','Video uploaded'].includes(text(element)));
  return {channelId,channelTitle:text(one('#channel-name'))||text(one('#entity-name'))||channelId,
    dialog:!!dialog,fileInput:!!dialog?.querySelector('ytcp-uploads-file-picker input[type="file"]'),
    detailsReady:!!dialog&&!!titleBox&&!!radio(kidName),detailsSet:!!titleBox&&text(titleBox)===data.title&&selected(kidName),
    nextEnabled:!!next&&!disabled(next),doneVisible:!!done&&visible(done),doneEnabled:!!done&&!disabled(done),
    unlistedVisible:!!radio('UNLISTED'),unlisted:selected('UNLISTED')&&!selected('PUBLIC')&&!selected('PRIVATE'),
    videoId:ids.length===1?ids[0]:'',editorId,filename:text(one('#original-filename')),
    privacy:['По ссылке','Unlisted'].includes(visibility)?'unlisted':visibility,
    processed:processed&&!dialog,saved:!!save&&disabled(save)&&!dialog,url:location.origin+location.pathname,uploadComplete,finished,
    uploadMenu:!!exactButton(['Добавить видео','Upload videos']),
    createVisible:!!exactButton(['Создать','Create']),error:errors.join(' · ').slice(0,300)};
}
