'use strict';
document.querySelector('#login-form').onsubmit=async event=>{
  event.preventDefault();const form=event.currentTarget,button=form.querySelector('button'),error=document.querySelector('#login-error');
  button.disabled=true;error.textContent='';
  try{
    const response=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'ProspectLocal'},body:JSON.stringify(Object.fromEntries(new FormData(form)))});
    const result=await response.json();if(!response.ok)throw Error(result.error||'Não foi possível entrar.');
    form.elements.password.value='';window.location.replace('/');
  }catch(e){error.textContent=e.message;}finally{button.disabled=false;}
};
