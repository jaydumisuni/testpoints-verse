from __future__ import annotations
import argparse,hashlib,json,math,os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
import cv2,numpy as np
from PIL import Image

EXT={'.webp','.png','.jpg','.jpeg'}; MIN_GROUP=20; Q=88

def read(p):
    return cv2.imdecode(np.fromfile(str(p),np.uint8),cv2.IMREAD_COLOR)
def save(p,im):
    p.parent.mkdir(parents=True,exist_ok=True)
    ok,b=cv2.imencode('.webp',im,[cv2.IMWRITE_WEBP_QUALITY,Q])
    if not ok: raise RuntimeError('webp encode')
    b.tofile(str(p))
def sha(p):
    h=hashlib.sha256(); h.update(p.read_bytes()); return h.hexdigest()
def mean_image(ps):
    choose=ps if len(ps)<=180 else [ps[int(i*len(ps)/180)] for i in range(180)]
    acc=None;n=0
    for p in choose:
        im=read(p)
        if im is None: continue
        if acc is None: acc=np.zeros_like(im,dtype=np.float32)
        acc+=im;n+=1
    return (acc/n).astype(np.uint8)
def watermark_mask(mean):
    h,w=mean.shape[:2]; g=cv2.cvtColor(mean,cv2.COLOR_BGR2GRAY); blur=cv2.GaussianBlur(g,(0,0),max(14.,w*.029),max(14.,w*.029));
    m=((blur.astype(np.int16)-g.astype(np.int16))>9).astype(np.uint8)*255
    m=cv2.morphologyEx(m,cv2.MORPH_OPEN,np.ones((2,2),np.uint8)); m=cv2.dilate(m,np.ones((3,3),np.uint8),1)
    n,lab,stats,_=cv2.connectedComponentsWithStats(m,8); out=np.zeros_like(m); area=h*w
    for i in range(1,n):
        x,y,ww,hh,a=map(int,stats[i])
        if 8<=a<=area*.03 and ww>=2 and hh>=2: out[lab==i]=255
    ratio=np.count_nonzero(out)/area
    if not .04<=ratio<=.28: raise RuntimeError(f'unsafe mask {ratio:.3f}')
    return out
def labels(img):
    h,w=img.shape[:2]; hsv=cv2.cvtColor(img,cv2.COLOR_BGR2HSV); white=cv2.inRange(hsv,np.array([0,0,220]),np.array([180,60,255])); n,lab,stats,_=cv2.connectedComponentsWithStats(white,8); out=[]
    for i in range(1,n):
        x,y,ww,hh,a=map(int,stats[i]); fill=a/max(1,ww*hh)
        if a>=120 and ww>=25 and hh>=10 and x>3 and y>3 and x+ww<w-3 and y+hh<h-3 and fill>.35 and ww/hh>1.2 and ww<.65*w and hh<.25*h:
            out.append((max(0,x-4),max(0,y-4),min(w,x+ww+4),min(h,y+hh+4)))
    return out
def gap(a,b):
    ax,ay,aw,ah=a; bx,by,bw,bh=b; dx=max(bx-(ax+aw),ax-(bx+bw),0);dy=max(by-(ay+ah),ay-(by+bh),0);return math.hypot(dx,dy)
def yellow(img):
    hsv=cv2.cvtColor(img,cv2.COLOR_BGR2HSV); m=cv2.inRange(hsv,np.array([15,100,120]),np.array([45,255,255])); n,lab,stats,_=cv2.connectedComponentsWithStats(m,8); comps=[]
    for i in range(1,n):
        x,y,w,h,a=map(int,stats[i]);
        if a>=300 and w>=20 and h>=20: comps.append((a,i,x,y,w,h))
    if not comps:return np.zeros_like(m)
    comps.sort(reverse=True); _,idx,x,y,w,h=comps[0]; keep=np.zeros_like(m);keep[lab==idx]=255; box=(x,y,w,h)
    for _,i,x,y,w,h in comps[1:]:
        if gap(box,(x,y,w,h))<=45: keep[lab==i]=255
    return keep
def clean(img,m):
    out=cv2.inpaint(img,m,3,cv2.INPAINT_TELEA)
    for x0,y0,x1,y1 in labels(img):out[y0:y1,x0:x1]=img[y0:y1,x0:x1]
    ym=yellow(img)
    if np.count_nonzero(ym):out[ym>0]=np.median(img[ym>0],axis=0).astype(np.uint8)
    hsv=cv2.cvtColor(out,cv2.COLOR_BGR2HSV); seed=((hsv[:,:,1]>45)|(hsv[:,:,2]<150)).astype(np.uint8); n,lab,stats,_=cv2.connectedComponentsWithStats(seed,8); keep=np.zeros_like(seed);h,w=seed.shape
    for i in range(1,n):
        x,y,ww,hh,a=map(int,stats[i]);
        if a>=max(150,int(h*w*.0015)):keep[lab==i]=1
    ys,xs=np.where(keep>0)
    if len(xs)>=100:
        x0=max(0,int(xs.min())-12);x1=min(w,int(xs.max())+13);y0=max(0,int(ys.min())-12);y1=min(h,int(ys.max())+13);outside=np.ones((h,w),bool);outside[y0:y1,x0:x1]=False;bg=(hsv[:,:,1]<60)&(hsv[:,:,2]>150);out[outside&bg]=255
    return out
def logo_load(p):
    im=Image.open(p).convert('RGBA');return im.crop(im.getbbox())
def brand(img,logo):
    rgb=cv2.cvtColor(img,cv2.COLOR_BGR2RGB);base=Image.fromarray(rgb).convert('RGBA');w,h=base.size;tw=max(38,min(96,int(w*.075)));th=max(1,int(tw*logo.height/logo.width));lg=logo.resize((tw,th),Image.Resampling.LANCZOS);a=np.array(lg);a[:,:,3]=(a[:,:,3].astype(float)*.88).clip(0,255).astype(np.uint8);lg=Image.fromarray(a,'RGBA');margin=max(8,int(min(w,h)*.015));g=cv2.cvtColor(img,cv2.COLOR_BGR2GRAY);gx=cv2.Sobel(g,cv2.CV_32F,1,0,ksize=3);gy=cv2.Sobel(g,cv2.CV_32F,0,1,ksize=3);e=cv2.magnitude(gx,gy);cand=[]
    for name,x,y in [('tl',margin,margin),('tr',w-margin-tw,margin),('bl',margin,h-margin-th),('br',w-margin-tw,h-margin-th)]:
        if x<0 or y<0:continue
        hsv=cv2.cvtColor(img[y:y+th,x:x+tw],cv2.COLOR_BGR2HSV);cand.append((float(np.mean(e[y:y+th,x:x+tw]))+float(np.mean(hsv[:,:,1]>120))*80,name,x,y))
    _,name,x,y=min(cand);base.alpha_composite(lg,(x,y));return cv2.cvtColor(np.array(base.convert('RGB')),cv2.COLOR_RGB2BGR),name
def one(root,out,p,mask,logo):
    rel=p.relative_to(root/'test-points');dest=out/'test-points'/rel.with_suffix('.webp')
    if dest.exists():
        with Image.open(p) as s,Image.open(dest) as d:return {'source':str(p.relative_to(root)).replace('\\','/'),'clean':str(dest.relative_to(root)).replace('\\','/'),'status':'cleaned','method':'resume-existing','logo_corner':'existing','source_sha256':sha(p),'clean_sha256':sha(dest),'source_size':list(s.size),'clean_size':list(d.size)}
    im=read(p);cl,corner=brand(clean(im,mask),logo);save(dest,cl);return {'source':str(p.relative_to(root)).replace('\\','/'),'clean':str(dest.relative_to(root)).replace('\\','/'),'status':'cleaned','method':'chimera-mask-inpaint-margin-clean','logo_corner':corner,'source_sha256':sha(p),'clean_sha256':sha(dest),'source_size':[im.shape[1],im.shape[0]],'clean_size':[cl.shape[1],cl.shape[0]]}
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--root',default='.');ap.add_argument('--logo',default='assets/clean-brand-ghost.png');ap.add_argument('--workers',type=int,default=min(20,max(4,os.cpu_count() or 4)));a=ap.parse_args();root=Path(a.root).resolve();out=root/'clean';logo=logo_load(root/a.logo);ps=[p for p in (root/'test-points').rglob('*') if p.is_file() and p.suffix.lower() in EXT];groups=defaultdict(list)
    for p in ps:
        try:size=Image.open(p).size
        except:continue
        if size[0]==760:groups[size].append(p)
    profiles={};
    for size,items in sorted(groups.items()):
        if len(items)>=MIN_GROUP:
            try:profiles[size]=watermark_mask(mean_image(sorted(items)))
            except Exception as e:print('[skip]',size,len(items),e)
    tasks=[(p,profiles[size]) for size in profiles for p in groups[size]];results=[]
    with ThreadPoolExecutor(max_workers=min(20,max(1,a.workers))) as ex:
        fut=[ex.submit(one,root,out,p,m,logo) for p,m in tasks]
        for i,f in enumerate(as_completed(fut),1):results.append(f.result());print(f'clean {i}/{len(fut)}') if i%100==0 or i==len(fut) else None
    (out/'test-points').mkdir(parents=True,exist_ok=True);(out/'isp-pinouts').mkdir(parents=True,exist_ok=True)
    manifest={'schema_version':'testpoints-verse.clean.v1','policy':{'originals_untouched':True,'clean_tree':'clean/','only_verified_profiles_admitted':True},'summary':{'source_images':len(ps),'cleaned':len(results),'failed':0,'chimera_profiles':{f'{w}x{h}':len(groups[(w,h)]) for w,h in profiles},'isp_status':'pending detail-preserving QA'},'records':sorted(results,key=lambda r:r['source'])};(out/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8');(out/'README.md').write_text('# Clean derivative library\n\nOriginal source trees remain untouched. `clean/test-points/` contains verified CHIMERA watermark-reduced WebP derivatives with THETECHGUY corner branding. `clean/isp-pinouts/` is reserved until the EasyJTAG cleanup passes detail-preservation QA.\n',encoding='utf-8')
if __name__=='__main__':main()
