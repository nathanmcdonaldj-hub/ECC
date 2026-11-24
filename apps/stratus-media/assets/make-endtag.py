from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageChops, ImageEnhance
import subprocess
W,H=1080,1920; NAVY=(0,12,64)
src=Image.open('/mnt/project-files/stratus-house-logo/Stratus House logo.jpg').convert('RGB')
b=src.convert('L').point(lambda v:255 if v>90 else 0).getbbox(); logo=src.crop((b[0]-20,b[1]-20,b[2]+20,b[3]+20))
s=640; logo=logo.resize((s,int(logo.size[1]*s/logo.size[0])),Image.LANCZOS)
LY=560; LX=(W-s)//2
mask=Image.new('L',(W,H),0)
lm=logo.convert('L').point(lambda v:0 if v<45 else (255 if v>95 else int((v-45)*255/50)))
mask.paste(lm,(LX,LY))
f1=ImageFont.truetype('/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf',100)
f2=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',62)
d=ImageDraw.Draw(mask)
y=LY+logo.size[1]+105; t='STRATUS HOUSE'; sp=4
ws=[d.textlength(ch,font=f1) for ch in t]; x=(W-(sum(ws)+sp*(len(t)-1)))/2
for ch,w in zip(t,ws): d.text((x,y),ch,font=f1,fill=255); x+=w+sp
d.rectangle([W/2-260,y+140,W/2+260,y+144],fill=255)
tex=Image.open('liquid-gold.png').convert('RGB')
tex=ImageEnhance.Contrast(tex).enhance(1.15)
T=tex.resize((1500,1500),Image.LANCZOS)
big=Image.new('RGB',(3000,3000)); 
for i in range(2):
    for j in range(2):
        tt=T if (i+j)%2==0 else T.transpose(Image.FLIP_LEFT_RIGHT)
        big.paste(tt,(i*1500,j*1500))
bg=Image.new('RGB',(W,H),NAVY)
shadow=mask.filter(ImageFilter.GaussianBlur(8)).point(lambda v:int(v*0.85))
glow=mask.filter(ImageFilter.GaussianBlur(28)).point(lambda v:int(v*0.25))
bg.paste((200,150,40),(0,0),glow)
bg.paste((0,0,15),(6,10),shadow)
edge=ImageChops.subtract(mask.filter(ImageFilter.MaxFilter(5)),mask)
sub_y=y+182
def frame(fi):
    off=int(fi*3)
    fill=big.crop((300+off,200+off//2,300+off+W,200+off//2+H))
    fr=bg.copy(); fr.paste((90,60,10),(0,0),edge); fr.paste(fill,(0,0),mask)
    dd=ImageDraw.Draw(fr); dd.text(((W-dd.textlength('Like & follow for more',font=f2))/2,sub_y),'Like & follow for more',font=f2,fill=(255,255,255))
    if 10<=fi<=44:
        pos=-400+(W+H*0.4+800)*(fi-10)/34
        band=Image.new('L',(W,H),0); ImageDraw.Draw(band).polygon([(pos,0),(pos+90,0),(pos+90-H*0.4,H),(pos-H*0.4,H)],fill=255)
        m=ImageChops.multiply(band.filter(ImageFilter.GaussianBlur(30)),mask).point(lambda v:int(v*0.7))
        fr=Image.composite(Image.new('RGB',(W,H),(255,250,225)),fr,m)
    return fr
frame(0).save('endtag.png')
N=60
p=subprocess.Popen(['ffmpeg','-y','-loglevel','error','-f','rawvideo','-pix_fmt','rgb24','-s','%dx%d'%(W,H),'-r','30','-i','-',
 '-f','lavfi','-i','anullsrc=r=48000:cl=stereo','-t','2','-vf','fade=in:0:9,format=yuv420p','-c:v','libx264','-crf','18','-preset','medium','-c:a','aac','-b:a','128k','-shortest','endtag.mp4'],stdin=subprocess.PIPE)
for fi in range(N):
    fr=frame(fi)
    z=1+0.04*fi/N; cw,ch=int(W/z),int(H/z)
    fr=fr.crop(((W-cw)//2,(H-ch)//2,(W-cw)//2+cw,(H-ch)//2+ch)).resize((W,H),Image.BICUBIC)
    p.stdin.write(fr.tobytes())
p.stdin.close(); p.wait()
