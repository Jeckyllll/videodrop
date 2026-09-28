/* Shared settings for the Chrome Studio uploader. No Google Cloud configuration. */
window.VideoDropYouTube=class VideoDropYouTube {
  constructor(root,{request,load,save,wake}){
    this.request=request;this.load=load;this.save=save;this.wake=wake||(()=>{});this.timer=null;
    this.value={provider:'studio',enabled:false,deleteLocal:false,channelId:'',madeForKids:false};
    this.account={ready:false,connected:false,channelId:''};
    root.innerHTML=`<div class="youtube-settings">
      <label class="youtube-check"><input data-youtube="enabled" type="checkbox"> Загружать на YouTube</label>
      <div data-youtube="options" class="youtube-options" hidden>
        <p class="youtube-privacy">Доступ: <strong>только по ссылке</strong></p>
        <div class="youtube-account"><span data-youtube="account"></span><button data-youtube="connect" type="button">Подключить Chrome</button><button data-youtube="disconnect" type="button" hidden>Отключить</button></div>
        <p data-youtube="connection" class="youtube-hint"></p>
        <label class="youtube-label">Аудитория видео<select data-youtube="audience"><option value="no">Не предназначено для детей</option><option value="yes">Предназначено для детей</option></select></label>
        <p class="youtube-hint">Видео отправляется через обычную вкладку YouTube Studio. Оставьте Chrome и вкладку загрузки открытыми до завершения.</p>
      </div>
      <label class="youtube-check youtube-delete"><input data-youtube="delete" type="checkbox" disabled> Удалить локальную копию после загрузки</label>
      <p data-youtube="hint" class="youtube-hint"></p>
      <p data-youtube="message" class="youtube-message" role="status" aria-live="polite" hidden></p>
    </div>`;
    this.get=name=>root.querySelector(`[data-youtube="${name}"]`);
    this.get('enabled').onchange=async()=>{this.value.enabled=this.get('enabled').checked;if(!this.value.enabled)this.value.deleteLocal=false;this.render();this.persist();if(this.value.enabled)await this.refresh();};
    this.get('delete').onchange=()=>{this.value.deleteLocal=this.value.enabled&&this.get('delete').checked;this.persist();};
    this.get('audience').onchange=()=>{this.value.madeForKids=this.get('audience').value==='yes';this.persist();};
    this.get('connect').onclick=()=>this.connect();
    this.get('disconnect').onclick=async()=>{try{this.accept(await this.request('studio/disconnect',{}));this.tell('Канал отключён от VideoDrop. Вход в YouTube сохранён в Chrome.');}catch(error){this.tell(error.message);}};
  }
  async init(){try{const value=await this.load();if(value)this.set(value);}catch{}this.render();await this.refresh();return this;}
  set(value){this.value={provider:'studio',enabled:value.provider==='studio'&&value.enabled===true,deleteLocal:value.provider==='studio'&&value.enabled===true&&value.deleteLocal===true,channelId:typeof value.channelId==='string'?value.channelId:'',madeForKids:value.madeForKids===true};this.render();}
  tell(message){this.get('message').textContent=message;this.get('message').hidden=!message;}
  persist(){Promise.resolve(this.save({...this.value})).catch(()=>this.tell('Не удалось запомнить настройки.'));}
  render(){
    this.get('enabled').checked=this.value.enabled;this.get('options').hidden=!this.value.enabled;
    this.get('delete').disabled=!this.value.enabled;this.get('delete').checked=this.value.enabled&&this.value.deleteLocal;
    this.get('audience').value=this.value.madeForKids?'yes':'no';
    this.get('account').textContent=this.account.connected?'Канал: '+this.account.channelTitle:'Канал ещё не подключён';
    this.get('connect').textContent=this.account.connecting?'Ждём Chrome…':this.account.connected?'Сменить канал':'Подключить Chrome';
    this.get('connect').disabled=!!this.account.connecting;this.get('disconnect').hidden=!this.account.connected;
    this.get('connection').textContent=this.account.ready?'Расширение Chrome на связи.':'Откройте расширение VideoDrop в Chrome. После обновления подтвердите разрешение «Отладчик».';
    this.get('hint').textContent=this.value.enabled?'Удаление — только после обработки ролика и проверки доступа «По ссылке». При ошибке файл останется на Mac.':'Локальная копия остаётся на Mac.';
  }
  accept(account){
    if(this.value.channelId!==account.channelId)this.value.deleteLocal=false;
    this.account=account;this.value.channelId=account.channelId||'';this.render();this.persist();
    if(this.timer&&!account.connecting){clearInterval(this.timer);this.timer=null;this.tell(account.connected?'Канал подключён. Можно скачивать и отправлять видео.':'Подключение не завершено. Проверьте окно YouTube Studio и повторите.');}
  }
  async refresh(){try{this.accept(await this.request('studio/status'));}catch(error){this.tell(error.message);}}
  async connect(){
    try{
      this.accept(await this.request('studio/connect',{}));this.wake(this.account.extensionId);
      this.tell('Откроется YouTube Studio. Если нужно, войдите в свой аккаунт. Расширение проверяет очередь каждые 30 секунд.');
      clearInterval(this.timer);this.timer=setInterval(()=>this.refresh(),2000);
    }catch(error){this.tell(error.message);}
  }
  async options(){
    if(!this.value.enabled)return {provider:'studio',enabled:false,deleteLocal:false,channelId:'',madeForKids:false};
    const account=await this.request('studio/status');
    if(!account.connected)throw new Error('Нажмите «Подключить Chrome» или снимите галочку YouTube.');
    if(account.channelId!==this.value.channelId){this.accept(account);throw new Error('Канал изменился. Проверьте настройки удаления и повторите.');}
    if(!account.ready)throw new Error('Откройте Chrome и расширение VideoDrop 1.3.');
    this.accept(account);return {...this.value};
  }
};
