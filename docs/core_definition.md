\# NIZAM – Core Definition



\## 1. Amaç



NIZAM, kritik tesis çevre güvenliği için gerçek zamanlı durumsal farkındalık (COP) sağlayan, sensör bağımsız bir platform çekirdeğidir.

Farklı sensör ve yazılım ajanlarından (kamera, radar, simülasyon vb.) gelen olay (event) verilerini ortak bir formatta toplar, bu verileri iz (track) seviyesinde birleştirir ve harita üzerinde anlık operasyonel durum farkındalığı oluşturur.

Sistem; tanımlı bölge (zone) ihlali, riskli yakınlaşma ve önceden tanımlı kural setlerine göre alarm ve uyarı üretmeye temel sağlar.

Çekirdek hedef, düşük gecikmeli veri işleme, izlenebilir loglama ve modüler entegrasyon yapısı ile sahaya uyarlanabilir bir altyapı sunmaktır.

Bu aşamada NIZAM, nihai imha veya angajman kararı vermez; yalnızca durumsal farkındalık ve karar destek süreçleri için veri üretir.



\## 2. Girdi (Input)



NIZAM çekirdeği, sistem dışındaki sensörler veya yazılım ajanları tarafından üretilen olay (event) verilerini girdi olarak alır.

Her event, sensör tipinden bağımsız olarak ortak bir veri şemasına uygun olmak zorundadır.

Çekirdek, ham sensör verisi (ör. video akışı, radar sinyali) işlemez; yalnızca önceden işlenmiş ve konum bilgisi içeren olay verilerini kabul eder.

Event verileri, gerçek zamanlı veya simüle edilmiş kaynaklardan gelebilir ve geçerli bir zaman damgası (timestamp) içermelidir.

Tüm girişler, sistem tarafından tanımlı doğrulama ve temel bütünlük kontrollerinden geçirilir.



\### Event Tanımı



Event, belirli bir zaman ve konumda algılanan bir nesneye veya duruma ait temel bilgileri temsil eder.

Bir event, en azından aşağıdaki çekirdek alanları içermelidir:



\- event\_id  

\- sensor\_id  

\- sensor\_type  

\- timestamp  

\- latitude  

\- longitude  

\- object\_type  



\## 3. İşleme (Processing)



NIZAM çekirdeği, aldığı event verilerini merkezi bir işlem hattında değerlendirir.

Her event, zaman ve konum bilgisine göre mevcut izler (track) ile ilişkilendirilir veya yeni bir iz oluşturur.

Belirli bir süre boyunca güncellenmeyen veya geçerliliğini yitiren event’lere ait izler otomatik olarak sonlandırılır.



Çekirdek işlem hattı aşağıdaki temel adımlardan oluşur:

\- Event doğrulama ve normalizasyon

\- Event–track eşleştirme veya yeni track oluşturma

\- Track durumunun güncellenmesi (konum, zaman, nesne türü)

\- Tanımlı kural setlerine göre değerlendirme (ör. bölge ihlali)

\- Gerekli durumlarda alarm veya uyarı tetikleme



İşleme süreci, sensör tipinden ve veri kaynağından bağımsız olarak çalışır.

Tüm kararlar deterministik kurallara dayanır ve izlenebilir şekilde loglanır.



\### Track Yönetimi



Track, bir veya birden fazla event’in zaman içinde ilişkilendirilmesiyle oluşan mantıksal bir izdir.

Her track, tek bir nesneyi (insan, araç, drone veya bilinmeyen) temsil eder.

Track’ler event akışına bağlı olarak güncellenir; birleştirilmez ve bölünmez.

Belirli bir süre boyunca yeni event almayan track’ler pasif hale getirilir ve sistemden kaldırılır.



\## 4. Çıktı (Output)



NIZAM çekirdeği, işlenen event ve track verilerinden türetilen çıktıları Ortak Operasyonel Durum (COP) ve kayıt (log) mekanizmaları üzerinden üretir.

Çıktılar, operatörlerin durumsal farkındalığını artırmak ve karar destek süreçlerini beslemek amacıyla yapılandırılmıştır.



Temel çıktılar şunlardır:

\- Harita üzerinde güncel track gösterimi (konum, tür, zaman)

\- Tanımlı bölge (zone) ihlali ve kural tetiklemelerine bağlı alarm ve uyarılar

\- Track yaşam döngüsüne ilişkin olay kayıtları (oluşma, güncelleme, sonlanma)



Tüm çıktılar, gerçek zamanlı izleme yanında geriye dönük analiz ve yeniden oynatma (replay) senaryolarını destekleyecek şekilde loglanır.

Çekirdek çıktı mekanizması, kullanıcı arayüzü veya bildirim kanallarından bağımsızdır ve farklı sunum katmanları tarafından tüketilebilir.



