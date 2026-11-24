from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageChops
import subprocess, math
W,H=1080,1920; NAVY=(0,12,64)
src=Image.open('/mnt/project-files/stratus-house-logo/Stratus House logo.jpg').convert('RGB')
b=src.convert('L').point(lambda v:255 if v>90 else 0).getbbox(); logo=src.crop((b[0]-20,b[1]-20,b[2]+20,b[3]+20))
s=640; logo=logo.resize((s,int(logo.size[1]*s/logo.size[0])),Image.LANCZOS)
LY=560; base=Image.new('RGB',(W,H),NAVY); base.paste(logo,((W-s)//2,LY))
# text mask
f1=ImageFont.truetype('/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf',96)
f2=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',62)
tm=Image.new('L',(W,H),0); d=ImageDraw.Draw(tm)
y=LY+logo.size[1]+110; t='STRATUS HOUSE'; sp=4
ws=[d.textlength(ch,font=f1) for ch in t]; x=(W-(sum(ws)+sp*(len(t)-1)))/2
for ch,w in zip(t,ws): d.text((x,y),ch,font=f1,fill=255); x+=w+sp
d.rectangle([W/2-260,y+138,W/2+260,y+141],fill=255)
tb=tm.getbbox()
# metallic gradient: dark gold -> bright highlight -> deep gold -> light
grad=Image.new('RGB',(W,H)); gd=ImageDraw.Draw(grad)
stops=[(0,(150,105,20)),(0.35,(255,236,150)),(0.5,(255,250,215)),(0.62,(215,160,40)),(1,(250,210,100))]
y0,y1=tb[1],tb[3]-6
for yy in range(H):
    tt=min(1,max(0,(yy-y0)/(y1-y0)))
    for i in range(len(stops)-1):
        a,ca=stops[i]; bb,cb=stops[i+1]
        if a<=tt<=bb:
            k=(tt-a)/(bb-a); col=tuple(int(ca[j]+(cb[j]-ca[j])*k) for j in range(3)); break
    gd.line([(0,yy),(W,yy)],fill=col)
shadow=tm.filter(ImageFilter.GaussianBlur(6))
base.paste((0,0,20),(0,0),shadow.point(lambda v:int(v*0.8)))
base.paste(grad,(0,0),tm)
ImageDraw.Draw(base).text(((W-ImageDraw.Draw(base).textlength('Like & follow for more',font=f2))/2,y+180),'Like & follow for more',font=f2,fill=(255,255,255))
# gold mask (logo letters + title) for glint
gold=base.convert('L').point(lambda v:255 if v>70 else 0)
gold=ImageChops.subtract(gold, Image.new('L',(W,H),0)); 
# exclude white subtitle from glint
wm=Image.new('L',(W,H),0); ImageDraw.Draw(wm).rectangle([0,y+170,W,y+260],fill=255)
gold=ImageChops.subtract(gold,wm)
base.save('endtag.png')
N=60
p=subprocess.Popen(['ffmpeg','-y','-loglevel','error','-f','rawvideo','-pix_fmt','rgb24','-s','%dx%d'%(W,H),'-r','30','-i','-',
 '-f','lavfi','-i','anullsrc=r=48000:cl=stereo','-t','2','-vf','fade=in:0:9,format=yuv420p','-c:v','libx264','-crf','18','-preset','medium','-c:a','aac','-b:a','128k','-shortest','endtag.mp4'],stdin=subprocess.PIPE)
for fi in range(N):
    fr=base
    # glint sweeps frames 10..45 diagonally
    if 8<=fi<=46:
        pos=-400+(W+H*0.4+800)*(fi-8)/38
        band=Image.new('L',(W,H),0); bd=ImageDraw.Draw(band)
        bd.polygon([(pos,0),(pos+120,0),(pos+120-H*0.4,H),(pos-H*0.4,H)],fill=255)
        band=band.filter(ImageFilter.GaussianBlur(35))
        m=ImageChops.multiply(band,gold)
        fr=Image.composite(Image.new('RGB',(W,H),(255,252,235)),base,m.point(lambda v:int(v*0.85)))
    # gentle zoom
    z=1+0.04*fi/N; cw,ch=int(W/z),int(H/z)
    fr=fr.crop(((W-cw)//2,(H-ch)//2,(W-cw)//2+cw,(H-ch)//2+ch)).resize((W,H),Image.BICUBIC)
    p.stdin.write(fr.tobytes())
    if fi==26: fr.save('glint.png')
p.stdin.close(); p.wait()
